import sqlite3
from pathlib import Path

from oncall_eval.database import EvaluationDatabase
from oncall_eval.rubric import RUBRIC_DIMENSIONS, RubricRecord, write_rubric_record
from oncall_eval.runner import load_run_record, write_run_record

FIXTURE = Path("eval/fixtures/dev-001.runrecord.json")


def _seed_run(root: Path) -> None:
    write_run_record(root / "runs" / "fixture-dev-001.json", load_run_record(FIXTURE))


def _seed_rubric(root: Path) -> None:
    rubric: RubricRecord = {
        "run_id": "fixture-dev-001",
        "rubric_version": "investigation-v1",
        "scorer_type": "human",
        "scorer_id": "reviewer-1",
        "dimensions": {dimension: 4 for dimension in RUBRIC_DIMENSIONS},
        "explanations": {"evidence_grounding": "Every conclusion cites evidence."},
        "evidence_refs": {"evidence_grounding": ["event:evidence-1"]},
    }
    write_rubric_record(root / "rubrics" / "fixture-dev-001.json", rubric)


def _seed_retrospective(root: Path) -> None:
    record = dict(load_run_record(FIXTURE))
    record |= {
        "run_id": "retrospective-dev-001",
        "source_incident_id": "saved-incident-001",
        "passed": False,
        "independently_verified_recovery": False,
        "verified_recovery": False,
        "failure_reasons": ["system recovery claim was not independently observed"],
    }
    write_run_record(root / "runs" / "retrospective-dev-001.json", record)


def _seed_error(root: Path) -> None:
    record = dict(load_run_record(FIXTURE))
    record |= {
        "run_id": "error-dev-001",
        "passed": False,
        "error": "SignalTimeout: detection was not observed",
        "failure_reasons": ["system error: SignalTimeout"],
    }
    write_run_record(root / "runs" / "error-dev-001.json", record)


def test_sync_persists_run_rubric_and_normalized_dimensions(tmp_path: Path):
    _seed_run(tmp_path)
    _seed_rubric(tmp_path)
    database = EvaluationDatabase(tmp_path)

    assert database.sync() == []

    assert database.path.is_file()
    assert database.table_counts() == {
        "evaluation_runs": 1,
        "rubric_scores": 1,
        "rubric_dimensions": 7,
    }
    run, rubric = database.get_run("fixture-dev-001")
    assert run["passed"] is True
    assert run["tool_budget"] == 10
    assert run["execution_trace"]["model_calls"] == 1
    assert rubric is not None
    assert rubric["score"] == 28
    c1 = database.summary()["by_config"][1]
    assert c1["average_model_calls"] == 1
    assert c1["average_tokens"] == 0
    assert c1["average_rubric_score"] == 28


def test_database_survives_a_dashboard_process_restart(tmp_path: Path):
    _seed_run(tmp_path)
    first_process = EvaluationDatabase(tmp_path)
    first_process.sync()

    second_process = EvaluationDatabase(tmp_path)

    assert second_process.summary()["total_runs"] == 1
    assert second_process.list_runs()[0]["run_id"] == "fixture-dev-001"


def test_retrospective_artifacts_do_not_pollute_live_pass_rate(tmp_path: Path):
    _seed_run(tmp_path)
    _seed_retrospective(tmp_path)
    _seed_error(tmp_path)
    database = EvaluationDatabase(tmp_path)
    database.sync()

    summary = database.summary()
    listing = {item["run_id"]: item for item in database.list_runs()}

    assert summary["total_runs"] == 3
    assert summary["scored_runs"] == 1
    assert summary["retrospective_runs"] == 1
    assert summary["errored_runs"] == 1
    assert summary["passed"] == 1
    assert summary["failed"] == 0
    assert summary["pass_rate"] == 1.0
    assert summary["by_config"][1]["runs"] == 1
    assert listing["retrospective-dev-001"]["evaluation_mode"] == "retrospective"
    assert listing["error-dev-001"]["evaluation_mode"] == "error"
    assert listing["fixture-dev-001"]["evaluation_mode"] == "live"


def test_older_retrospective_attempt_is_superseded_and_not_rubric_pending(tmp_path: Path):
    _seed_retrospective(tmp_path)
    older = tmp_path / "runs" / "retrospective-dev-001.json"
    older.touch()
    newer_record = dict(load_run_record(older)) | {"run_id": "retrospective-dev-002"}
    write_run_record(tmp_path / "runs" / "retrospective-dev-002.json", newer_record)
    database = EvaluationDatabase(tmp_path)
    database.sync()

    listing = {item["run_id"]: item for item in database.list_runs()}

    assert listing["retrospective-dev-001"]["superseded"] is True
    assert listing["retrospective-dev-001"]["evaluation_mode"] == "superseded"
    assert listing["retrospective-dev-002"]["superseded"] is False
    assert database.get_run("retrospective-dev-001")[0]["superseded"] is True
    assert database.summary()["rubric_eligible"] == 1


def test_database_is_rebuildable_from_canonical_artifacts(tmp_path: Path):
    _seed_run(tmp_path)
    first = EvaluationDatabase(tmp_path)
    first.sync()
    first.path.unlink()

    rebuilt = EvaluationDatabase(tmp_path)
    assert rebuilt.sync() == []

    assert rebuilt.summary()["passed"] == 1


def test_invalid_and_orphan_artifacts_are_not_indexed(tmp_path: Path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "broken.json").write_text("{}", encoding="utf-8")
    _seed_rubric(tmp_path)
    database = EvaluationDatabase(tmp_path)

    errors = database.sync()

    assert {error["file"] for error in errors} == {
        "broken.json",
        "fixture-dev-001.json",
    }
    assert database.table_counts()["evaluation_runs"] == 0


def test_sqlite_schema_records_artifact_hashes_and_failure_reasons(tmp_path: Path):
    _seed_run(tmp_path)
    database = EvaluationDatabase(tmp_path)
    database.sync()

    with sqlite3.connect(database.path) as connection:
        row = connection.execute(
            "SELECT artifact_sha256, failure_reasons_json FROM evaluation_runs"
        ).fetchone()

    assert len(row[0]) == 64
    assert row[1] == "[]"
