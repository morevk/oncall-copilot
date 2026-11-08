"""Durable SQLite storage for incident snapshots and their ordered events."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class IncidentDatabase:
    """Persist the public incident contract without coupling to agent internals."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.Lock()
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
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    config_id TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS incidents_created_idx
                    ON incidents(created_at DESC);
                CREATE INDEX IF NOT EXISTS incidents_status_idx
                    ON incidents(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS incident_events (
                    incident_id TEXT NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    time TEXT NOT NULL,
                    message TEXT NOT NULL,
                    PRIMARY KEY (incident_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS feed_events (
                    sequence INTEGER PRIMARY KEY,
                    incident_id TEXT NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
                    type TEXT NOT NULL,
                    time TEXT NOT NULL,
                    incident_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS alert_keys (
                    alert_key TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL
                );

                PRAGMA user_version = 1;
                """
            )

    def load(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
        with self._mutex, self._connect() as connection:
            incidents = [
                json.loads(row["snapshot_json"])
                for row in connection.execute(
                    "SELECT snapshot_json FROM incidents ORDER BY created_at"
                )
            ]
            feed = [
                {
                    "id": row["sequence"],
                    "type": row["type"],
                    "time": row["time"],
                    "incident": json.loads(row["incident_json"]),
                }
                for row in connection.execute(
                    "SELECT sequence, type, time, incident_json FROM feed_events ORDER BY sequence"
                )
            ]
            keys = {
                row["alert_key"]: row["incident_id"]
                for row in connection.execute("SELECT alert_key, incident_id FROM alert_keys")
            }
        return incidents, feed, keys

    def save_event(
        self,
        snapshot: dict[str, Any],
        event: dict[str, Any],
        feed_event: dict[str, Any],
    ) -> None:
        compact_snapshot = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
        compact_feed_incident = json.dumps(
            feed_event["incident"], separators=(",", ":"), sort_keys=True
        )
        with self._mutex, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO incidents (
                    incident_id, mode, status, created_at, updated_at, config_id, snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    mode = excluded.mode,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    config_id = excluded.config_id,
                    snapshot_json = excluded.snapshot_json
                """,
                (
                    snapshot["incident_id"],
                    snapshot["mode"],
                    snapshot["status"],
                    snapshot["created_at"],
                    snapshot["updated_at"],
                    snapshot["config_id"],
                    compact_snapshot,
                ),
            )
            connection.execute(
                """
                INSERT INTO incident_events (incident_id, sequence, type, time, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot["incident_id"],
                    event["id"],
                    event["type"],
                    event["time"],
                    event["message"],
                ),
            )
            connection.execute(
                """
                INSERT INTO feed_events (sequence, incident_id, type, time, incident_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    feed_event["id"],
                    snapshot["incident_id"],
                    feed_event["type"],
                    feed_event["time"],
                    compact_feed_incident,
                ),
            )

    def reserve_alert(self, key: str, incident_id: str) -> None:
        with self._mutex, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO alert_keys (alert_key, incident_id) VALUES (?, ?)
                ON CONFLICT(alert_key) DO UPDATE SET incident_id = excluded.incident_id
                """,
                (key, incident_id),
            )
