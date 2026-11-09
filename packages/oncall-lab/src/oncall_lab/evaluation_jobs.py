"""Operational state for evaluations started from the Control Center.

Run JSON under the artifacts directory stays the source of truth. This
database is not rebuilt from those files, and it is not the dashboard index.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oncall_eval.execution import (
    EvaluationEvent,
    EvaluationExecutionResult,
    EvaluationRefused,
    EvaluationRequest,
    EvaluationSettings,
    execute_evaluation,
    resolve_cases,
)
from oncall_eval.http_client import IncidentApiError
from oncall_eval.injector import FaultInjectionError
from oncall_eval.live_lock import LiveEvaluationLocked, live_evaluation_lock_held
from oncall_eval.signals import SignalReadError

logger = logging.getLogger(__name__)

ACKNOWLEDGEMENT = "INJECT LIVE FAULTS"
KINDS = ("preflight", "deterministic")
TOPOLOGIES = ("c1_single", "c2_team")
TERMINAL = ("succeeded", "failed", "interrupted")
_SAFE_FAILURE = "evaluation failed before completion"
_INTERRUPTED = "interrupted by process restart; a live run is not resumed"
_KNOWN_FAILURES = (
    EvaluationRefused,
    FaultInjectionError,
    IncidentApiError,
    SignalReadError,
    LiveEvaluationLocked,
    OSError,
    ValueError,
)

Execute = Callable[
    [EvaluationRequest, EvaluationSettings, Callable[[EvaluationEvent], None]],
    EvaluationExecutionResult,
]
Clock = Callable[[], str]


class AcknowledgementRequired(Exception):
    """The live-fault phrase was missing or not exact."""


class JobRejected(ValueError):
    """The request is not a supported evaluation."""


class JobConflict(RuntimeError):
    """Another evaluation already owns the lab."""


class JobUnavailable(RuntimeError):
    """The server-owned case files cannot be read."""


class JobNotFound(KeyError):
    """No job has this id."""


@dataclass(frozen=True)
class EvaluationJob:
    job_id: str
    kind: str
    config_id: str
    case_ids: tuple[str, ...]
    state: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    message: str
    run_ids: tuple[str, ...]
    error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "config_id": self.config_id,
            "case_ids": list(self.case_ids),
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "message": self.message,
            "run_ids": list(self.run_ids),
            "error": self.error,
        }


@dataclass(frozen=True)
class JobEvent:
    sequence: int
    job_id: str
    timestamp: str
    phase: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "phase": self.phase,
            "message": self.message,
        }


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _clip(text: str) -> str:
    cleaned = " ".join(text.split())
    return cleaned[:500]


def _plain_case_ids(case_ids: tuple[str, ...]) -> bool:
    if len(set(case_ids)) != len(case_ids):
        return False
    for case_id in case_ids:
        if not case_id or case_id in {".", ".."} or ".." in case_id:
            return False
        if any(char in case_id for char in "/\\\n\r\t"):
            return False
    return True


class JobStore:
    """SQLite job metadata. Callers share one store across the lab process."""

    def __init__(self, path: Path, *, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or _timestamp
        self._mutex = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._mutex, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('preflight', 'deterministic')),
                    config_id TEXT NOT NULL,
                    case_ids_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('queued', 'running', 'succeeded', 'failed', 'interrupted')
                    ),
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    message TEXT NOT NULL,
                    run_ids_json TEXT NOT NULL,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    sequence INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    message TEXT NOT NULL,
                    PRIMARY KEY (job_id, sequence)
                );
                """
            )
            self._interrupt_unfinished(connection)

    def _interrupt_unfinished(self, connection: sqlite3.Connection) -> None:
        now = self.clock()
        rows = connection.execute(
            "SELECT job_id FROM jobs WHERE state IN ('queued', 'running')"
        ).fetchall()
        for row in rows:
            connection.execute(
                """
                UPDATE jobs
                SET state = 'interrupted', finished_at = ?, message = ?, error = ?
                WHERE job_id = ?
                """,
                (now, _INTERRUPTED, _INTERRUPTED, row["job_id"]),
            )
            self._append(connection, row["job_id"], "interrupted", _INTERRUPTED, now)

    def create(
        self,
        *,
        kind: str,
        config_id: str,
        case_ids: tuple[str, ...],
        lock_held: Callable[[], bool],
    ) -> EvaluationJob:
        with self._mutex, self._connect() as connection:
            if lock_held():
                raise JobConflict("another live evaluation holds the lab")
            active = connection.execute(
                "SELECT job_id FROM jobs WHERE state IN ('queued', 'running') LIMIT 1"
            ).fetchone()
            if active is not None:
                raise JobConflict(f"an evaluation is already active: {active['job_id']}")
            now = self.clock()
            job_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, kind, config_id, case_ids_json, state, created_at,
                    started_at, finished_at, message, run_ids_json, error
                ) VALUES (?, ?, ?, ?, 'queued', ?, NULL, NULL, 'queued', '[]', NULL)
                """,
                (job_id, kind, config_id, json.dumps(list(case_ids)), now),
            )
            return self._job(connection, job_id)

    def mark_running(self, job_id: str) -> bool:
        """Move a queued job to running. Return False when it must not start."""
        with self._mutex, self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None or row["state"] != "queued":
                return False
            connection.execute(
                """
                UPDATE jobs SET state = 'running', started_at = ?, message = 'running'
                WHERE job_id = ?
                """,
                (self.clock(), job_id),
            )
            return True

    def append_event(self, job_id: str, phase: str, message: str) -> JobEvent:
        text = _clip(message)
        with self._mutex, self._connect() as connection:
            self._require(connection, job_id)
            event = self._append(connection, job_id, phase, text, self.clock())
            connection.execute("UPDATE jobs SET message = ? WHERE job_id = ?", (text, job_id))
            return event

    def finish(
        self,
        job_id: str,
        *,
        state: str,
        message: str,
        run_ids: tuple[str, ...] = (),
        error: str | None = None,
        record_event: bool = False,
    ) -> EvaluationJob:
        if state not in TERMINAL:
            raise ValueError(f"not a terminal state: {state}")
        text = _clip(message)
        stored_error = None if error is None else _clip(error)
        with self._mutex, self._connect() as connection:
            current = self._require(connection, job_id)
            if current["state"] not in {"queued", "running", "interrupted"}:
                return self._job(connection, job_id)
            now = self.clock()
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, finished_at = ?, message = ?, run_ids_json = ?, error = ?
                WHERE job_id = ?
                """,
                (state, now, text, json.dumps(list(run_ids)), stored_error, job_id),
            )
            if record_event:
                phase = "interrupted" if state == "interrupted" else "complete"
                self._append(connection, job_id, phase, text, now)
            return self._job(connection, job_id)

    def get(self, job_id: str) -> EvaluationJob:
        with self._mutex, self._connect() as connection:
            self._require(connection, job_id)
            return self._job(connection, job_id)

    def list_jobs(self) -> tuple[EvaluationJob, ...]:
        with self._mutex, self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id FROM jobs ORDER BY created_at DESC, job_id DESC"
            ).fetchall()
            return tuple(self._job(connection, row["job_id"]) for row in rows)

    def events(self, job_id: str) -> tuple[JobEvent, ...]:
        return self.events_after(job_id, 0)

    def events_after(self, job_id: str, sequence: int) -> tuple[JobEvent, ...]:
        with self._mutex, self._connect() as connection:
            self._require(connection, job_id)
            rows = connection.execute(
                """
                SELECT sequence, job_id, timestamp, phase, message
                FROM events
                WHERE job_id = ? AND sequence > ?
                ORDER BY sequence
                """,
                (job_id, sequence),
            ).fetchall()
        return tuple(self._event(row) for row in rows)

    def active_job_id(self) -> str | None:
        with self._mutex, self._connect() as connection:
            row = connection.execute(
                "SELECT job_id FROM jobs WHERE state IN ('queued', 'running') LIMIT 1"
            ).fetchone()
        return None if row is None else str(row["job_id"])

    def _append(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        phase: str,
        message: str,
        timestamp: str,
    ) -> JobEvent:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_seq FROM events WHERE job_id = ?",
            (job_id,),
        ).fetchone()["next_seq"]
        connection.execute(
            """
            INSERT INTO events (job_id, sequence, timestamp, phase, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, sequence, timestamp, phase, message),
        )
        return JobEvent(sequence, job_id, timestamp, phase, message)

    def _require(self, connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        return row

    def _job(self, connection: sqlite3.Connection, job_id: str) -> EvaluationJob:
        row = self._require(connection, job_id)
        return EvaluationJob(
            job_id=row["job_id"],
            kind=row["kind"],
            config_id=row["config_id"],
            case_ids=tuple(json.loads(row["case_ids_json"])),
            state=row["state"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            message=row["message"],
            run_ids=tuple(json.loads(row["run_ids_json"])),
            error=row["error"],
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> JobEvent:
        return JobEvent(
            sequence=row["sequence"],
            job_id=row["job_id"],
            timestamp=row["timestamp"],
            phase=row["phase"],
            message=row["message"],
        )


class EvaluationJobService:
    """Validate a browser request and run at most one evaluation in-process."""

    def __init__(
        self,
        database: Path,
        settings: EvaluationSettings,
        *,
        execute: Execute | None = None,
        lock_held: Callable[[], bool] | None = None,
        clock: Clock | None = None,
        keepalive_seconds: float = 15.0,
    ) -> None:
        self.store = JobStore(database, clock=clock)
        self.settings = settings
        self.execute = execute or execute_evaluation
        self.lock_held = lock_held or (lambda: live_evaluation_lock_held(settings.lock_file))
        self.keepalive_seconds = keepalive_seconds
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    def capabilities(self) -> dict[str, Any]:
        return {
            "kinds": list(KINDS),
            "topologies": list(TOPOLOGIES),
            "judge_available": False,
            "active_job_id": self.store.active_job_id(),
            "acknowledgement": ACKNOWLEDGEMENT,
        }

    def submit(
        self,
        *,
        kind: str,
        config_id: str,
        case_ids: tuple[str, ...],
        acknowledgement: str | None,
    ) -> EvaluationJob:
        if kind not in KINDS:
            raise JobRejected(f"unsupported evaluation kind {kind!r}")
        if config_id not in TOPOLOGIES:
            raise JobRejected(
                f"unsupported evaluation topology {config_id!r}; "
                f"choose one of {', '.join(TOPOLOGIES)}"
            )
        if not _plain_case_ids(case_ids):
            raise JobRejected("case_ids must be plain case identifiers")
        if kind == "deterministic" and acknowledgement != ACKNOWLEDGEMENT:
            raise AcknowledgementRequired("type the exact acknowledgement INJECT LIVE FAULTS")
        try:
            resolve_cases(self.settings, case_ids)
        except ValueError as exc:
            text = str(exc)
            if text.startswith("unknown case ids"):
                raise JobRejected(text) from exc
            raise JobUnavailable(text) from exc
        except OSError as exc:
            raise JobUnavailable("evaluation cases are unavailable") from exc
        job = self.store.create(
            kind=kind,
            config_id=config_id,
            case_ids=case_ids,
            lock_held=self.lock_held,
        )
        self._launch(job.job_id)
        return job

    def join(self, timeout: float = 2.0) -> None:
        with self._thread_lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _launch(self, job_id: str) -> None:
        # Non-daemon so a process shutdown can finish flag cleanup inside the run.
        thread = threading.Thread(
            target=self._run,
            args=(job_id,),
            name=f"eval-job-{job_id}",
            daemon=False,
        )
        with self._thread_lock:
            self._thread = thread
        thread.start()

    def _run(self, job_id: str) -> None:
        if not self.store.mark_running(job_id):
            return
        job = self.store.get(job_id)
        request = EvaluationRequest(kind=job.kind, config_id=job.config_id, case_ids=job.case_ids)

        def emit(event: EvaluationEvent) -> None:
            self.store.append_event(job_id, event.phase, event.message)

        try:
            result = self.execute(request, self.settings, emit)
        except _KNOWN_FAILURES as exc:
            text = _clip(str(exc))
            self.store.finish(
                job_id,
                state="failed",
                message=text,
                error=text,
                record_event=True,
            )
            return
        except Exception:
            logger.exception("evaluation job %s failed", job_id)
            self.store.finish(
                job_id,
                state="failed",
                message=_SAFE_FAILURE,
                error=_SAFE_FAILURE,
                record_event=True,
            )
            return
        message = self.store.get(job_id).message
        self.store.finish(
            job_id,
            state="succeeded" if result.passed else "failed",
            message=message,
            run_ids=tuple(result.run_ids),
            error=None if result.passed else message,
            record_event=False,
        )


def settings_from_environment() -> EvaluationSettings:
    """Host defaults stay local. Compose overrides them with in-network URLs."""
    root = Path(os.environ.get("ONCALL_EVAL_ARTIFACTS", "artifacts"))
    return EvaluationSettings(
        cases_dir=Path(os.environ.get("ONCALL_EVAL_CASES", "eval/dev")),
        manifest_path=Path(os.environ.get("ONCALL_MANIFEST", "eval/faults/manifest.yaml")),
        flagd_config=Path(os.environ.get("FLAGD_CONFIG", "deploy/flagd/demo.flagd.json")),
        prometheus_url=os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090"),
        api_url=(
            os.environ.get("ONCALL_API_URL")
            or os.environ.get("ONCALL_SERVICE_URL")
            or "http://127.0.0.1:8000"
        ),
        artifacts_dir=root / "runs",
        lock_file=Path(
            os.environ.get("ONCALL_EVAL_LOCK_FILE", root / "locks" / "live-evaluation.lock")
        ),
        fail_under=1.0,
    )


def jobs_database_from_environment() -> Path:
    if value := os.environ.get("ONCALL_EVAL_JOBS_DATABASE"):
        return Path(value)
    root = Path(os.environ.get("ONCALL_EVAL_ARTIFACTS", "artifacts"))
    return root / "evaluation-jobs.sqlite3"


def service_from_environment() -> EvaluationJobService:
    return EvaluationJobService(jobs_database_from_environment(), settings_from_environment())
