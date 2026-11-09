"""Rebuildable SQLite index over immutable evaluation JSON artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oncall_eval.rubric import RubricRecord, load_rubric_record
from oncall_eval.runner import load_run_record

SCHEMA_VERSION = 1


def _artifact_error(path: Path, exc: Exception) -> dict[str, str]:
    return {"file": path.name, "error": str(exc)}


def _modified_at(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


class EvaluationDatabase:
    """Indexes completed artifacts for dashboard queries; JSON remains canonical."""

    def __init__(self, root: Path, database_path: Path | None = None) -> None:
        self.root = root.resolve()
        self.runs_dir = self.root / "runs"
        self.rubrics_dir = self.root / "rubrics"
        self.path = (database_path or self.root / "evaluation.sqlite3").resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS evaluation_runs (
                    run_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    fault_id TEXT NOT NULL,
                    config_id TEXT NOT NULL,
                    complexity TEXT NOT NULL,
                    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
                    correct_root_cause INTEGER NOT NULL CHECK (correct_root_cause IN (0, 1)),
                    correct_action_proposed INTEGER NOT NULL
                        CHECK (correct_action_proposed IN (0, 1)),
                    system_reported_recovery INTEGER NOT NULL
                        CHECK (system_reported_recovery IN (0, 1)),
                    independently_verified_recovery INTEGER NOT NULL
                        CHECK (independently_verified_recovery IN (0, 1)),
                    cleanup_verified INTEGER NOT NULL CHECK (cleanup_verified IN (0, 1)),
                    verified_recovery INTEGER NOT NULL CHECK (verified_recovery IN (0, 1)),
                    within_tool_budget INTEGER NOT NULL CHECK (within_tool_budget IN (0, 1)),
                    harmful_actions INTEGER NOT NULL,
                    tool_calls INTEGER NOT NULL,
                    tokens INTEGER NOT NULL,
                    usd REAL NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    failure_reasons_json TEXT NOT NULL,
                    artifact_path TEXT NOT NULL UNIQUE,
                    artifact_sha256 TEXT NOT NULL,
                    artifact_modified_ns INTEGER NOT NULL,
                    artifact_modified_at TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    indexed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS evaluation_runs_config_idx
                    ON evaluation_runs(config_id, complexity, passed);
                CREATE INDEX IF NOT EXISTS evaluation_runs_modified_idx
                    ON evaluation_runs(artifact_modified_ns DESC);

                CREATE TABLE IF NOT EXISTS rubric_scores (
                    run_id TEXT PRIMARY KEY REFERENCES evaluation_runs(run_id) ON DELETE CASCADE,
                    rubric_version TEXT NOT NULL,
                    scorer_type TEXT NOT NULL,
                    scorer_id TEXT NOT NULL,
                    total_score INTEGER NOT NULL CHECK (total_score BETWEEN 0 AND 35),
                    artifact_path TEXT NOT NULL UNIQUE,
                    artifact_sha256 TEXT NOT NULL,
                    artifact_modified_at TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    indexed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rubric_dimensions (
                    run_id TEXT NOT NULL REFERENCES rubric_scores(run_id) ON DELETE CASCADE,
                    dimension TEXT NOT NULL,
                    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 5),
                    explanation TEXT,
                    evidence_refs_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, dimension)
                );
                """
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def sync(self) -> list[dict[str, str]]:
        """Validate and transactionally mirror the current artifact directories."""
        errors: list[dict[str, str]] = []
        runs: list[tuple[Path, dict[str, Any], bytes]] = []
        run_ids: set[str] = set()
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                raw = path.read_bytes()
                record = dict(load_run_record(path))
                if record["run_id"] in run_ids:
                    raise ValueError(f"duplicate RunRecord for run_id {record['run_id']!r}")
                run_ids.add(record["run_id"])
                runs.append((path.resolve(), record, raw))
            except (OSError, ValueError) as exc:
                errors.append(_artifact_error(path, exc))

        rubrics: list[tuple[Path, RubricRecord, bytes]] = []
        rubric_run_ids: set[str] = set()
        for path in sorted(self.rubrics_dir.glob("*.json")):
            try:
                raw = path.read_bytes()
                record = load_rubric_record(path)
                if record["run_id"] not in run_ids:
                    raise ValueError(f"rubric has no matching RunRecord: {record['run_id']!r}")
                if record["run_id"] in rubric_run_ids:
                    raise ValueError(f"duplicate rubric for run_id {record['run_id']!r}")
                rubric_run_ids.add(record["run_id"])
                rubrics.append((path.resolve(), record, raw))
            except (OSError, ValueError) as exc:
                errors.append(_artifact_error(path, exc))

        indexed_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for path, record, raw in runs:
                stat = path.stat()
                connection.execute(
                    """
                    INSERT INTO evaluation_runs (
                        run_id, case_id, fault_id, config_id, complexity, passed,
                        correct_root_cause, correct_action_proposed, system_reported_recovery,
                        independently_verified_recovery, cleanup_verified, verified_recovery,
                        within_tool_budget, harmful_actions, tool_calls, tokens, usd, latency_ms,
                        failure_reasons_json, artifact_path, artifact_sha256,
                        artifact_modified_ns, artifact_modified_at, record_json, indexed_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(run_id) DO UPDATE SET
                        case_id=excluded.case_id,
                        fault_id=excluded.fault_id,
                        config_id=excluded.config_id,
                        complexity=excluded.complexity,
                        passed=excluded.passed,
                        correct_root_cause=excluded.correct_root_cause,
                        correct_action_proposed=excluded.correct_action_proposed,
                        system_reported_recovery=excluded.system_reported_recovery,
                        independently_verified_recovery=excluded.independently_verified_recovery,
                        cleanup_verified=excluded.cleanup_verified,
                        verified_recovery=excluded.verified_recovery,
                        within_tool_budget=excluded.within_tool_budget,
                        harmful_actions=excluded.harmful_actions,
                        tool_calls=excluded.tool_calls,
                        tokens=excluded.tokens,
                        usd=excluded.usd,
                        latency_ms=excluded.latency_ms,
                        failure_reasons_json=excluded.failure_reasons_json,
                        artifact_path=excluded.artifact_path,
                        artifact_sha256=excluded.artifact_sha256,
                        artifact_modified_ns=excluded.artifact_modified_ns,
                        artifact_modified_at=excluded.artifact_modified_at,
                        record_json=excluded.record_json,
                        indexed_at=excluded.indexed_at
                    """,
                    (
                        record["run_id"],
                        record["case_id"],
                        record["fault_id"],
                        record["config_id"],
                        record["complexity"],
                        record["passed"],
                        record["correct_root_cause"],
                        record["correct_action_proposed"],
                        record["system_reported_recovery"],
                        record["independently_verified_recovery"],
                        record["cleanup_verified"],
                        record["verified_recovery"],
                        record["within_tool_budget"],
                        record["harmful_actions"],
                        record["tool_calls"],
                        record["tokens"],
                        record["usd"],
                        record["latency_ms"],
                        json.dumps(record["failure_reasons"]),
                        str(path),
                        hashlib.sha256(raw).hexdigest(),
                        stat.st_mtime_ns,
                        _modified_at(path),
                        json.dumps(record, sort_keys=True),
                        indexed_at,
                    ),
                )

            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                connection.execute(
                    f"DELETE FROM evaluation_runs WHERE run_id NOT IN ({placeholders})",
                    tuple(sorted(run_ids)),
                )
            else:
                connection.execute("DELETE FROM evaluation_runs")

            for path, record, raw in rubrics:
                total = sum(record["dimensions"].values())
                connection.execute(
                    """
                    INSERT INTO rubric_scores (
                        run_id, rubric_version, scorer_type, scorer_id, total_score,
                        artifact_path, artifact_sha256, artifact_modified_at, record_json, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        rubric_version=excluded.rubric_version,
                        scorer_type=excluded.scorer_type,
                        scorer_id=excluded.scorer_id,
                        total_score=excluded.total_score,
                        artifact_path=excluded.artifact_path,
                        artifact_sha256=excluded.artifact_sha256,
                        artifact_modified_at=excluded.artifact_modified_at,
                        record_json=excluded.record_json,
                        indexed_at=excluded.indexed_at
                    """,
                    (
                        record["run_id"],
                        record["rubric_version"],
                        record["scorer_type"],
                        record["scorer_id"],
                        total,
                        str(path),
                        hashlib.sha256(raw).hexdigest(),
                        _modified_at(path),
                        json.dumps(record, sort_keys=True),
                        indexed_at,
                    ),
                )
                connection.execute(
                    "DELETE FROM rubric_dimensions WHERE run_id = ?", (record["run_id"],)
                )
                for dimension, score in record["dimensions"].items():
                    connection.execute(
                        """
                        INSERT INTO rubric_dimensions (
                            run_id, dimension, score, explanation, evidence_refs_json
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            record["run_id"],
                            dimension,
                            score,
                            record.get("explanations", {}).get(dimension),
                            json.dumps(record.get("evidence_refs", {}).get(dimension, [])),
                        ),
                    )

            if rubric_run_ids:
                placeholders = ",".join("?" for _ in rubric_run_ids)
                connection.execute(
                    f"DELETE FROM rubric_scores WHERE run_id NOT IN ({placeholders})",
                    tuple(sorted(rubric_run_ids)),
                )
            else:
                connection.execute("DELETE FROM rubric_scores")
        return errors

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT r.run_id, r.case_id, r.fault_id, r.config_id, r.complexity,
                       r.passed, r.tool_calls, r.latency_ms, r.artifact_modified_ns,
                       json_extract(r.record_json, '$.source_incident_id')
                           AS source_incident_id,
                       json_extract(r.record_json, '$.error') AS run_error,
                       s.total_score AS rubric_score
                FROM evaluation_runs AS r
                LEFT JOIN rubric_scores AS s ON s.run_id = r.run_id
                ORDER BY r.artifact_modified_ns DESC
                """
            ).fetchall()
        results = []
        seen_incidents: set[str] = set()
        for row in rows:
            source_incident_id = row["source_incident_id"]
            superseded = bool(source_incident_id and source_incident_id in seen_incidents)
            if source_incident_id:
                seen_incidents.add(source_incident_id)
            results.append(
                dict(row)
                | {
                    "passed": bool(row["passed"]),
                    "evaluation_mode": (
                        "superseded"
                        if superseded
                        else "retrospective"
                        if source_incident_id
                        else ("error" if row["run_error"] else "live")
                    ),
                    "superseded": superseded,
                    "rubric_max": 35,
                }
            )
        return results

    def get_run(self, run_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        with self._connect() as connection:
            run_row = connection.execute(
                "SELECT record_json, artifact_modified_ns FROM evaluation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            rubric_row = connection.execute(
                "SELECT record_json, total_score FROM rubric_scores WHERE run_id = ?", (run_id,)
            ).fetchone()
        run = json.loads(run_row["record_json"])
        run["artifact_modified_ns"] = run_row["artifact_modified_ns"]
        source_incident_id = run.get("source_incident_id")
        run["superseded"] = False
        if isinstance(source_incident_id, str):
            with self._connect() as connection:
                newest = connection.execute(
                    """
                    SELECT run_id FROM evaluation_runs
                    WHERE json_extract(record_json, '$.source_incident_id') = ?
                    ORDER BY artifact_modified_ns DESC, run_id DESC LIMIT 1
                    """,
                    (source_incident_id,),
                ).fetchone()
            run["superseded"] = newest is not None and newest["run_id"] != run_id
        rubric = None
        if rubric_row is not None:
            rubric = json.loads(rubric_row["record_json"])
            rubric |= {"score": rubric_row["total_score"], "max_score": 35}
        return run, rubric

    def summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT r.config_id, COUNT(*) AS runs, SUM(r.passed) AS passed,
                       AVG(r.tool_calls) AS average_tool_calls,
                       AVG(r.tokens) AS average_tokens,
                       AVG(r.usd) AS average_usd,
                       AVG(r.latency_ms) AS average_latency_ms,
                       AVG(json_extract(r.record_json, '$.execution_trace.model_calls'))
                           AS average_model_calls,
                       AVG(json_extract(r.record_json, '$.execution_trace.largest_prompt_tokens'))
                           AS average_largest_prompt_tokens,
                       AVG(s.total_score) AS average_rubric_score
                FROM evaluation_runs AS r
                LEFT JOIN rubric_scores AS s ON s.run_id = r.run_id
                WHERE json_extract(r.record_json, '$.source_incident_id') IS NULL
                  AND json_extract(r.record_json, '$.error') IS NULL
                GROUP BY r.config_id
                """
            ).fetchall()
            totals = connection.execute(
                """
                SELECT COUNT(*) AS total_runs,
                       COUNT(*) FILTER (
                           WHERE json_extract(record_json, '$.source_incident_id') IS NULL
                              AND json_extract(record_json, '$.error') IS NULL
                       ) AS scored_runs,
                       COUNT(*) FILTER (
                           WHERE json_extract(record_json, '$.source_incident_id') IS NULL
                             AND json_extract(record_json, '$.error') IS NOT NULL
                       ) AS errored_runs,
                       COUNT(*) FILTER (
                           WHERE json_extract(record_json, '$.source_incident_id') IS NOT NULL
                       ) AS retrospective_runs,
                       COALESCE(SUM(passed) FILTER (
                           WHERE json_extract(record_json, '$.source_incident_id') IS NULL
                             AND json_extract(record_json, '$.error') IS NULL
                       ), 0) AS passed,
                       0 AS unused
                FROM evaluation_runs
                """
            ).fetchone()
            rubric_totals = connection.execute(
                """
                WITH ranked AS (
                    SELECT run_id, record_json,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(
                                   json_extract(record_json, '$.source_incident_id'),
                                   'run:' || run_id
                               )
                               ORDER BY artifact_modified_ns DESC, run_id DESC
                           ) AS attempt_rank
                    FROM evaluation_runs
                )
                SELECT COUNT(*) FILTER (
                           WHERE json_extract(r.record_json, '$.error') IS NULL
                       ) AS rubric_eligible,
                       COUNT(s.run_id) FILTER (
                           WHERE json_extract(r.record_json, '$.error') IS NULL
                       ) AS rubric_scored
                FROM ranked AS r
                LEFT JOIN rubric_scores AS s ON s.run_id = r.run_id
                WHERE r.attempt_rank = 1
                """
            ).fetchone()
        grouped = {row["config_id"]: dict(row) for row in rows}
        by_config = []
        for config_id in ("c0_workflow", "c1_single", "c2_team"):
            values = grouped.get(config_id)
            runs = int(values["runs"]) if values else 0
            passed = int(values["passed"]) if values else 0
            by_config.append(
                {
                    "config_id": config_id,
                    "runs": runs,
                    "passed": passed,
                    "pass_rate": passed / runs if runs else None,
                    "average_tool_calls": values["average_tool_calls"] if values else None,
                    "average_tokens": values["average_tokens"] if values else None,
                    "average_usd": values["average_usd"] if values else None,
                    "average_latency_ms": values["average_latency_ms"] if values else None,
                    "average_model_calls": values["average_model_calls"] if values else None,
                    "average_largest_prompt_tokens": (
                        values["average_largest_prompt_tokens"] if values else None
                    ),
                    "average_rubric_score": values["average_rubric_score"] if values else None,
                }
            )
        total_runs = int(totals["total_runs"])
        scored_runs = int(totals["scored_runs"])
        passed = int(totals["passed"])
        return {
            "total_runs": total_runs,
            "scored_runs": scored_runs,
            "retrospective_runs": int(totals["retrospective_runs"]),
            "errored_runs": int(totals["errored_runs"]),
            "passed": passed,
            "failed": scored_runs - passed,
            "pass_rate": passed / scored_runs if scored_runs else None,
            "rubric_eligible": int(rubric_totals["rubric_eligible"]),
            "rubric_scored": int(rubric_totals["rubric_scored"]),
            "by_config": by_config,
        }

    def table_counts(self) -> dict[str, int]:
        """Small diagnostic used by health checks and tests."""
        with self._connect() as connection:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("evaluation_runs", "rubric_scores", "rubric_dimensions")
            }
