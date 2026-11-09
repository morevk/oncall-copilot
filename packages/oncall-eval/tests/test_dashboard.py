from pathlib import Path

from fastapi.testclient import TestClient
from oncall_eval.dashboard import create_app
from oncall_eval.rubric import RUBRIC_DIMENSIONS, RubricRecord, write_rubric_record
from oncall_eval.runner import load_run_record, write_run_record

FIXTURE = Path("eval/fixtures/dev-001.runrecord.json")


def _seed_run(root: Path) -> None:
    write_run_record(root / "runs" / "fixture-dev-001.json", load_run_record(FIXTURE))


def _seed_rubric(root: Path) -> None:
    record: RubricRecord = {
        "run_id": "fixture-dev-001",
        "rubric_version": "investigation-v1",
        "scorer_type": "human",
        "scorer_id": "reviewer-1",
        "dimensions": {dimension: 4 for dimension in RUBRIC_DIMENSIONS},
    }
    write_rubric_record(root / "rubrics" / "fixture-dev-001.json", record)


def test_dashboard_serves_read_only_console_and_empty_summary(tmp_path: Path):
    with TestClient(create_app(artifacts_dir=tmp_path)) as client:
        health = client.get("/healthz")
        page = client.get("/")
        script = client.get("/assets/app.js")
        summary = client.get("/api/summary")

    assert health.json() == {
        "status": "ok",
        "mode": "read-only",
        "database": str(tmp_path / "evaluation.sqlite3"),
    }
    assert "Evaluation runs" in page.text
    assert "C1 / C2 results" in page.text
    assert "Execution" in page.text
    assert "Retrospective" in page.text
    assert "Control center" in page.text
    assert "Incidents" in page.text
    assert "URLSearchParams" in script.text
    assert 'get("run")' in script.text
    assert "Source incident" in script.text
    assert "retrospective" in script.text
    assert summary.json()["total_runs"] == 0
    assert summary.json()["pass_rate"] is None


def test_dashboard_lists_and_aggregates_completed_run_artifacts(tmp_path: Path):
    _seed_run(tmp_path)

    with TestClient(create_app(artifacts_dir=tmp_path)) as client:
        listing = client.get("/api/runs").json()
        summary = client.get("/api/summary").json()
        detail = client.get("/api/runs/fixture-dev-001").json()

    assert listing["runs"][0]["passed"] is True
    assert listing["runs"][0]["rubric_score"] is None
    assert summary["passed"] == 1
    assert summary["rubric_eligible"] == 1
    assert summary["by_config"][1]["config_id"] == "c1_single"
    assert summary["by_config"][1]["pass_rate"] == 1.0
    assert detail["rubric"] is None
    assert detail["run"]["independently_verified_recovery"] is True
    assert detail["timeline"][-1] == {"label": "Fault cleanup verified", "ok": True}


def test_dashboard_joins_separate_rubric_by_run_id(tmp_path: Path):
    _seed_run(tmp_path)
    _seed_rubric(tmp_path)

    with TestClient(create_app(artifacts_dir=tmp_path)) as client:
        detail = client.get("/api/runs/fixture-dev-001").json()
        summary = client.get("/api/summary").json()

    assert detail["rubric"]["score"] == 28
    assert detail["rubric"]["max_score"] == 35
    assert summary["rubric_scored"] == 1
    assert summary["rubric_eligible"] == 1


def test_dashboard_ignores_and_reports_invalid_artifact(tmp_path: Path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "broken.json").write_text("{}", encoding="utf-8")

    with TestClient(create_app(artifacts_dir=tmp_path)) as client:
        listing = client.get("/api/runs").json()

    assert listing["runs"] == []
    assert listing["invalid_artifacts"][0]["file"] == "broken.json"


def test_unknown_run_is_404(tmp_path: Path):
    with TestClient(create_app(artifacts_dir=tmp_path)) as client:
        response = client.get("/api/runs/missing")

    assert response.status_code == 404
