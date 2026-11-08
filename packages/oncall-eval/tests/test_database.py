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
    assert rubric is not None
    assert rubric["score"] == 28


def test_database_survives_a_dashboard_process_restart(tmp_path: Path):
    _seed_run(tmp_path)
    first_process = EvaluationDatabase(tmp_path)
    first_process.sync()

    second_process = EvaluationDatabase(tmp_path)

    assert second_process.summary()["total_runs"] == 1
    assert second_process.list_runs()[0]["run_id"] == "fixture-dev-001"


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
