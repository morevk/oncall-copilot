"""Persistent background jobs for batch evaluation of saved incidents."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oncall_eval.batch import BatchEvaluationSummary, evaluate_saved_incidents
from oncall_eval.execution import EvaluationSettings, resolve_cases
from oncall_eval.judge import JudgeConfigurationError, JudgeSettings
from oncall_eval.judge.core import settings_from_environment as judge_settings_from_environment

logger = logging.getLogger(__name__)
TERMINAL = frozenset({"succeeded", "failed", "interrupted"})
_INTERRUPTED = "interrupted by process restart; batch evaluation was not resumed"
_SAFE_FAILURE = "batch evaluation failed before completion"


class BatchJobConflict(RuntimeError):
    """Another batch already owns the batch evaluator."""


class BatchJobUnavailable(RuntimeError):
    """The judge or saved incident source is unavailable."""


class BatchJobNotFound(KeyError):
    """No batch job has this id."""


@dataclass(frozen=True)
class BatchJob:
    job_id: str
    state: str
    re_evaluate: bool
    created_at: str
    started_at: str | None
    finished_at: str | None
    message: str
    summary: dict[str, Any]
    error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state,
            "re_evaluate": self.re_evaluate,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "message": self.message,
            "summary": self.summary,
            "error": self.error,
        }


@dataclass(frozen=True)
class BatchEvent:
    sequence: int
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


def _clip(value: str) -> str:
    return " ".join(value.split())[:500]


class BatchJobStore:
    def __init__(self, path: Path, *, clock: Callable[[], str] | None = None) -> None:
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
                CREATE TABLE IF NOT EXISTS batch_jobs (
                    job_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK (
                        state IN ('queued', 'running', 'succeeded', 'failed', 'interrupted')
                    ),
                    re_evaluate INTEGER NOT NULL CHECK (re_evaluate IN (0, 1)),
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    message TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS batch_events (
                    job_id TEXT NOT NULL REFERENCES batch_jobs(job_id),
                    sequence INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    message TEXT NOT NULL,
                    PRIMARY KEY (job_id, sequence)
                );
                """
            )
            now = self.clock()
            active = connection.execute(
                "SELECT job_id FROM batch_jobs WHERE state IN ('queued', 'running')"
            ).fetchall()
            for row in active:
                connection.execute(
                    """
                    UPDATE batch_jobs SET state = 'interrupted', finished_at = ?,
                        message = ?, error = ? WHERE job_id = ?
                    """,
                    (now, _INTERRUPTED, _INTERRUPTED, row["job_id"]),
                )
                self._append(connection, row["job_id"], "interrupted", _INTERRUPTED, now)

    def create(self, *, re_evaluate: bool) -> BatchJob:
        with self._mutex, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT job_id FROM batch_jobs WHERE state IN ('queued', 'running') LIMIT 1"
            ).fetchone()
            if active is not None:
                raise BatchJobConflict(f"a batch evaluation is already active: {active['job_id']}")
            job_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO batch_jobs (
                    job_id, state, re_evaluate, created_at, started_at, finished_at,
                    message, summary_json, error
                ) VALUES (?, 'queued', ?, ?, NULL, NULL, 'queued', '{}', NULL)
                """,
                (job_id, int(re_evaluate), self.clock()),
            )
            return self._job(connection, job_id)

    def mark_running(self, job_id: str) -> bool:
        with self._mutex, self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE batch_jobs SET state = 'running', started_at = ?, message = 'running'
                WHERE job_id = ? AND state = 'queued'
                """,
                (self.clock(), job_id),
            ).rowcount
            return changed == 1

    def append(self, job_id: str, phase: str, message: str) -> BatchEvent:
        text = _clip(message)
        with self._mutex, self._connect() as connection:
            self._require(connection, job_id)
            event = self._append(connection, job_id, phase, text, self.clock())
            connection.execute("UPDATE batch_jobs SET message = ? WHERE job_id = ?", (text, job_id))
            return event

    def finish(
        self,
        job_id: str,
        *,
        state: str,
        message: str,
        summary: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> BatchJob:
        if state not in TERMINAL:
            raise ValueError(f"invalid terminal batch state {state!r}")
        text = _clip(message)
        with self._mutex, self._connect() as connection:
            self._require(connection, job_id)
            connection.execute(
                """
                UPDATE batch_jobs SET state = ?, finished_at = ?, message = ?,
                    summary_json = ?, error = ? WHERE job_id = ?
                """,
                (
                    state,
                    self.clock(),
                    text,
                    json.dumps(dict(summary or {})),
                    None if error is None else _clip(error),
                    job_id,
                ),
            )
            self._append(connection, job_id, "complete", text, self.clock())
            return self._job(connection, job_id)

    def get(self, job_id: str) -> BatchJob:
        with self._mutex, self._connect() as connection:
            return self._job(connection, job_id)

    def events_after(self, job_id: str, sequence: int) -> tuple[BatchEvent, ...]:
        with self._mutex, self._connect() as connection:
            self._require(connection, job_id)
            rows = connection.execute(
                """
                SELECT sequence, timestamp, phase, message FROM batch_events
                WHERE job_id = ? AND sequence > ? ORDER BY sequence
                """,
                (job_id, sequence),
            ).fetchall()
        return tuple(
            BatchEvent(row["sequence"], row["timestamp"], row["phase"], row["message"])
            for row in rows
        )

    def events(self, job_id: str) -> tuple[BatchEvent, ...]:
        return self.events_after(job_id, 0)

    def active_job_id(self) -> str | None:
        with self._mutex, self._connect() as connection:
            row = connection.execute(
                "SELECT job_id FROM batch_jobs WHERE state IN ('queued', 'running') LIMIT 1"
            ).fetchone()
        return None if row is None else str(row["job_id"])

    def _append(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        phase: str,
        message: str,
        timestamp: str,
    ) -> BatchEvent:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM batch_events WHERE job_id = ?",
            (job_id,),
        ).fetchone()["value"]
        connection.execute(
            """
            INSERT INTO batch_events (job_id, sequence, timestamp, phase, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, sequence, timestamp, phase, message),
        )
        return BatchEvent(sequence, timestamp, phase, message)

    @staticmethod
    def _require(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM batch_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise BatchJobNotFound(job_id)
        return row

    def _job(self, connection: sqlite3.Connection, job_id: str) -> BatchJob:
        row = self._require(connection, job_id)
        return BatchJob(
            job_id=row["job_id"],
            state=row["state"],
            re_evaluate=bool(row["re_evaluate"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            message=row["message"],
            summary=json.loads(row["summary_json"]),
            error=row["error"],
        )


FetchIncidents = Callable[[], Sequence[Mapping[str, Any]]]
BatchExecute = Callable[..., BatchEvaluationSummary]
JudgeSettingsFactory = Callable[[], JudgeSettings]


class BatchEvaluationService:
    def __init__(
        self,
        database: Path,
        settings: EvaluationSettings,
        fetch_incidents: FetchIncidents,
        *,
        judge_settings: JudgeSettingsFactory,
        execute: BatchExecute = evaluate_saved_incidents,
        clock: Callable[[], str] | None = None,
        keepalive_seconds: float = 15.0,
    ) -> None:
        self.store = BatchJobStore(database, clock=clock)
        self.settings = settings
        self.fetch_incidents = fetch_incidents
        self.judge_settings = judge_settings
        self.execute = execute
        self.keepalive_seconds = keepalive_seconds
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    def capabilities(self) -> dict[str, Any]:
        try:
            configured = self.judge_settings()
            judge_available = bool(configured.model and configured.api_key)
            unavailable_reason = None
        except JudgeConfigurationError as exc:
            judge_available = False
            unavailable_reason = str(exc)
        return {
            "judge_available": judge_available,
            "unavailable_reason": unavailable_reason,
            "active_job_id": self.store.active_job_id(),
            "default_re_evaluate": False,
        }

    def submit(self, *, re_evaluate: bool) -> BatchJob:
        try:
            self.judge_settings()
        except JudgeConfigurationError as exc:
            raise BatchJobUnavailable(str(exc)) from exc
        job = self.store.create(re_evaluate=re_evaluate)
        thread = threading.Thread(
            target=self._run,
            args=(job.job_id,),
            name=f"batch-eval-{job.job_id}",
            daemon=False,
        )
        with self._thread_lock:
            self._thread = thread
        thread.start()
        return job

    def join(self, timeout: float = 5.0) -> None:
        with self._thread_lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, job_id: str) -> None:
        if not self.store.mark_running(job_id):
            return
        job = self.store.get(job_id)
        try:
            incidents = self.fetch_incidents()
            cases = resolve_cases(self.settings, ())
            configured_judge = self.judge_settings()
            summary = self.execute(
                incidents,
                cases,
                self.settings.artifacts_dir,
                configured_judge,
                re_evaluate=job.re_evaluate,
                emit=lambda phase, message: self.store.append(job_id, phase, message),
            )
        except Exception as exc:
            logger.exception("batch evaluation job %s failed", job_id)
            detail = (
                str(exc) if isinstance(exc, (OSError, ValueError, RuntimeError)) else _SAFE_FAILURE
            )
            self.store.finish(
                job_id,
                state="failed",
                message=detail,
                error=detail,
            )
            return
        message = (
            f"{summary.llm_created} LLM and {summary.deterministic_created} deterministic "
            f"evaluation(s) created; {summary.skipped_completed} already complete; "
            f"{summary.skipped_no_case} unmatched; {summary.failed} failed"
        )
        self.store.finish(
            job_id,
            state="succeeded" if summary.passed else "failed",
            message=message,
            summary=summary.as_dict(),
            error=None if summary.passed else message,
        )


def service_from_environment(
    settings: EvaluationSettings,
    fetch_incidents: FetchIncidents,
) -> BatchEvaluationService:
    root = settings.artifacts_dir.parent
    database = Path(os.environ.get("ONCALL_EVAL_JOBS_DATABASE", root / "evaluation-jobs.sqlite3"))
    rubric = Path(os.environ.get("ONCALL_EVAL_RUBRIC", "eval/rubrics/investigation-v1.yaml"))

    def configured_judge() -> JudgeSettings:
        return judge_settings_from_environment(
            rubric_path=rubric,
            artifacts_dir=root,
            incident_api_url=settings.api_url,
        )

    return BatchEvaluationService(
        database,
        settings,
        fetch_incidents,
        judge_settings=configured_judge,
    )
