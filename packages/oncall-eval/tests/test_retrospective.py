from pathlib import Path

import pytest
from oncall_eval.retrospective import (
    RetrospectiveEvaluationError,
    evaluate_persisted_incident,
)
from oncall_eval.runner import load_run_record


def _case():
    return {
        "case_id": "dev-001",
        "fault_id": "payment-failure",
        "complexity": "single",
        "variant": "100%",
        "alert": {
            "status": "firing",
            "labels": {"alertname": "CheckoutErrorRateHigh", "service": "checkout"},
            "annotations": {"summary": "Checkout failures"},
            "startsAt": "2026-11-02T00:00:00Z",
        },
        "expected": {
            "root_cause_services": ["payment"],
            "correct_action": {
                "action": "set_flag",
                "args": {"flag": "paymentFailure", "variant": "off"},
            },
            "max_tool_calls": 10,
            "harmful_actions": 0,
        },
    }


def _incident():
    action = {
        "action": "set_flag",
        "args": {"flag": "paymentFailure", "variant": "off"},
    }
    return {
        "incident_id": "saved-001",
        "terminal": True,
        "config_id": "c1_single",
        "created_at": "2026-11-08T10:00:00Z",
        "updated_at": "2026-11-08T10:00:03.250Z",
        "alert": {"labels": {"alertname": "CheckoutErrorRateHigh", "service": "checkout"}},
        "system_run": {
            "root_cause_services": ["payment"],
            "proposed_action": action,
            "executed_actions": [action],
            "tool_calls": 4,
            "tokens": 100,
            "usd": 0.01,
            "verified_recovery": True,
            "rolled_back": False,
            "model_ids": {"solo": "test/model"},
            "playbook_hash": None,
            "error": None,
        },
    }


def test_saved_incident_is_scored_without_inventing_recovery_witness(tmp_path: Path):
    result = evaluate_persisted_incident(
        _case(),
        _incident(),
        tmp_path,
        id_factory=lambda: "retrospective-test-001",
    )

    assert result.score.correct_root_cause is True
    assert result.score.correct_action_proposed is True
    assert result.score.passed is False
    assert result.record["source_incident_id"] == "saved-001"
    assert result.record["system_reported_recovery"] is True
    assert result.record["independently_verified_recovery"] is False
    assert result.record["cleanup_verified"] is False
    assert result.record["latency_ms"] == 3250
    assert result.record["failure_reasons"] == [
        "system recovery claim was not independently observed"
    ]
    assert load_run_record(result.artifact_path) == result.record


def test_saved_incident_must_match_the_selected_case(tmp_path: Path):
    incident = _incident()
    incident["alert"]["labels"]["service"] = "ad"

    with pytest.raises(RetrospectiveEvaluationError, match="does not match"):
        evaluate_persisted_incident(_case(), incident, tmp_path)


def test_saved_incident_must_be_terminal_and_have_system_evidence(tmp_path: Path):
    incident = _incident()
    incident["terminal"] = False
    with pytest.raises(RetrospectiveEvaluationError, match="must be terminal"):
        evaluate_persisted_incident(_case(), incident, tmp_path)

    incident["terminal"] = True
    incident["system_run"] = None
    with pytest.raises(RetrospectiveEvaluationError, match="no system_run"):
        evaluate_persisted_incident(_case(), incident, tmp_path)
