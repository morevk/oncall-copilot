"""Read-only HTTP API and static dashboard for completed evaluation artifacts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from oncall_eval.database import EvaluationDatabase

STATIC = Path(__file__).with_name("static")


class EvaluationRepository:
    """Synchronizes immutable artifacts into the rebuildable SQLite read model."""

    def __init__(self, root: Path, database_path: Path | None = None):
        self.database = EvaluationDatabase(root, database_path)

    def list(self) -> dict[str, Any]:
        errors = self.database.sync()
        return {
            "runs": self.database.list_runs(),
            "invalid_artifacts": errors,
        }

    def get(self, run_id: str) -> dict[str, Any]:
        self.database.sync()
        record, rubric = self.database.get_run(run_id)
        return {
            "run": record,
            "rubric": rubric,
            "timeline": self._timeline(record),
        }

    def summary(self) -> dict[str, Any]:
        errors = self.database.sync()
        return self.database.summary() | {"invalid_artifacts": errors}

    @staticmethod
    def _timeline(record: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"label": "Run artifact persisted", "ok": True},
            {"label": "Root cause matched", "ok": record["correct_root_cause"]},
            {"label": "Exact action matched", "ok": record["correct_action_proposed"]},
            {"label": "System reported recovery", "ok": record["system_reported_recovery"]},
            {
                "label": "Recovery independently observed",
                "ok": record["independently_verified_recovery"],
            },
            {"label": "Fault cleanup verified", "ok": record["cleanup_verified"]},
        ]


def create_app(*, artifacts_dir: Path | None = None, database_path: Path | None = None) -> FastAPI:
    root = artifacts_dir or Path(os.environ.get("ONCALL_EVAL_ARTIFACTS", "artifacts"))
    configured_database = database_path
    if configured_database is None and (value := os.environ.get("ONCALL_EVAL_DATABASE")):
        configured_database = Path(value)
    store = EvaluationRepository(root.resolve(), configured_database)
    app = FastAPI(title="On-Call Copilot Evaluation Dashboard", version="0.1.0")
    app.state.artifacts = store
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "mode": "read-only",
            "database": str(store.database.path),
        }

    @app.get("/api/runs")
    async def list_runs() -> dict[str, Any]:
        return store.list()

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        try:
            return store.get(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.get("/api/summary")
    async def summary() -> dict[str, Any]:
        return store.summary()

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run(
        "oncall_eval.dashboard.app:app",
        host=os.environ.get("ONCALL_EVAL_DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.environ.get("ONCALL_EVAL_DASHBOARD_PORT", "8030")),
        reload=False,
    )
