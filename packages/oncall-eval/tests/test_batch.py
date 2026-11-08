from pathlib import Path

from oncall_eval.batch import evaluate_saved_incidents, incident_evaluation_status
from oncall_eval.cases import load_case
from oncall_eval.judge import JudgeResult, JudgeSettings
from oncall_eval.retrospective import evaluate_persisted_incident
from oncall_eval.rubric import RUBRIC_DIMENSIONS, RubricRecord, write_rubric_record

ROOT = Path(__file__).parents[3]
CASE = load_case(ROOT / "eval/dev/001-payment-failure.json")
RUBRIC = ROOT / "eval/rubrics/investigation-v1.yaml"


def _incident(incident_id: str, *, alertname: str = "CheckoutErrorRateHigh") -> dict:
    action = {"action": "set_flag", "args": {"flag": "paymentFailure", "variant": "off"}}
    return {
        "incident_id": incident_id,
        "terminal": True,
        "status": "RECOVERED",
        "config_id": "c1_single",
        "created_at": "2026-11-08T10:00:00+00:00",
        "updated_at": "2026-11-08T10:00:03+00:00",
        "alert": {"labels": {"alertname": alertname, "service": "checkout"}},
        "state": {"evidence": ["payment errors"]},
        "system_run": {
            "root_cause_services": ["payment"],
            "proposed_action": action,
            "executed_actions": [action],
            "tool_calls": 2,
            "tokens": 10,
            "usd": 0.01,
            "verified_recovery": True,
            "rolled_back": False,
            "model_ids": {"solo": "openai/test-agent"},
            "playbook_hash": None,
            "error": None,
        },
    }


def _settings(tmp_path: Path) -> JudgeSettings:
    return JudgeSettings(
        model="google/test-judge",
        api_key="test-key",
        rubric_path=RUBRIC,
        artifacts_dir=tmp_path,
    )


def _fake_judge(run, _incident_value, settings):
    record: RubricRecord = {
        "run_id": run["run_id"],
        "rubric_version": "investigation-v1",
        "scorer_type": "llm",
        "scorer_id": settings.model,
        "dimensions": {dimension: 4 for dimension in RUBRIC_DIMENSIONS},
    }
    path = settings.artifacts_dir / "rubrics" / f"{run['run_id']}.json"
    write_rubric_record(path, record)
    return JudgeResult(record, path)


def test_batch_fills_missing_stages_and_skips_completed_and_unmatched(tmp_path: Path):
    completed = _incident("completed")
    deterministic_only = _incident("deterministic-only")
    completed_run = evaluate_persisted_incident(
        CASE, completed, tmp_path / "runs", id_factory=lambda: "completed-run"
    ).record
    write_rubric_record(
        tmp_path / "rubrics/completed-run.json",
        {
            "run_id": "completed-run",
            "rubric_version": "investigation-v1",
            "scorer_type": "llm",
            "scorer_id": "google/test-judge",
            "dimensions": {dimension: 4 for dimension in RUBRIC_DIMENSIONS},
        },
    )
    evaluate_persisted_incident(
        CASE, deterministic_only, tmp_path / "runs", id_factory=lambda: "pending-run"
    )
    events = []

    summary = evaluate_saved_incidents(
        [
            _incident("new"),
            completed,
            deterministic_only,
            _incident("unmatched", alertname="DifferentAlert"),
        ],
        [CASE],
        tmp_path / "runs",
        _settings(tmp_path),
        emit=lambda phase, message: events.append((phase, message)),
        judge=_fake_judge,
    )

    assert completed_run["run_id"] == "completed-run"
    assert summary.eligible == 4
    assert summary.deterministic_created == 1
    assert summary.llm_created == 2
    assert summary.skipped_completed == 1
    assert summary.skipped_no_case == 1
    assert summary.failed == 0
    assert "pending-run" in summary.run_ids
    status = incident_evaluation_status(
        [_incident("new"), completed, deterministic_only], tmp_path / "runs"
    )
    assert status["completed"] == {
        "deterministic_evaluated": True,
        "llm_evaluated": True,
    }
    assert status["deterministic-only"]["llm_evaluated"] is True
    assert any(phase == "skipped" and "unmatched" in message for phase, message in events)


def test_re_evaluate_creates_a_new_pair_without_overwriting_history(tmp_path: Path):
    incident = _incident("completed")
    run = evaluate_persisted_incident(
        CASE, incident, tmp_path / "runs", id_factory=lambda: "original-run"
    ).record
    _fake_judge(run, incident, _settings(tmp_path))

    summary = evaluate_saved_incidents(
        [incident],
        [CASE],
        tmp_path / "runs",
        _settings(tmp_path),
        re_evaluate=True,
        judge=_fake_judge,
    )

    assert summary.deterministic_created == 1
    assert summary.llm_created == 1
    assert summary.skipped_completed == 0
    assert len(list((tmp_path / "runs").glob("*.json"))) == 2
    assert len(list((tmp_path / "rubrics").glob("*.json"))) == 2
    assert (tmp_path / "runs/original-run.json").is_file()
    assert (tmp_path / "rubrics/original-run.json").is_file()
