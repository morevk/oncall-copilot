import json
from pathlib import Path

import pytest
from oncall_contracts.config import Topology
from oncall_contracts.faults import FaultSpec
from oncall_contracts.flagd import read_document, set_variant, write_document
from oncall_contracts.state import Alert
from oncall_eval.cases import EvalCase
from oncall_eval.injector import FlagdFaultInjector
from oncall_eval.runner import (
    EvaluationRunner,
    load_run_record,
    validate_run_record,
    write_run_record,
)
from oncall_eval.scoring import SystemRun
from oncall_eval.signals import Observation, SignalTimeout


def _case() -> EvalCase:
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
            "max_tool_calls": 5,
            "harmful_actions": 0,
        },
    }


def _fault() -> FaultSpec:
    return {
        "fault_id": "payment-failure",
        "enable": {
            "action": "set_flag",
            "args": {"flag": "paymentFailure", "variant": "100%"},
        },
        "disable": {
            "action": "set_flag",
            "args": {"flag": "paymentFailure", "variant": "off"},
        },
        "root_cause_services": ["payment"],
        "complexity": "single",
        "correct_action": {
            "action": "set_flag",
            "args": {"flag": "paymentFailure", "variant": "off"},
        },
        "detect": {"query": "failure_ratio", "operator": ">=", "threshold": 0.5},
        "recovery": {"query": "payment_rate", "operator": ">", "threshold": 0.0},
    }


def _injector(tmp_path: Path) -> FlagdFaultInjector:
    path = tmp_path / "flagd.json"
    path.write_text(
        json.dumps(
            {
                "flags": {
                    "paymentFailure": {
                        "defaultVariant": "off",
                        "variants": {"off": 0, "100%": 1},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return FlagdFaultInjector(path)


class ObservingPoller:
    def __init__(self, injector: FlagdFaultInjector):
        self.injector = injector

    def wait(self, _signal, *, phase: str) -> Observation:
        expected = "100%" if phase == "detection" else "off"
        actual = self.injector.current_variant("paymentFailure")
        if actual != expected:
            raise SignalTimeout(f"{phase} expected {expected!r}, found {actual!r}")
        return Observation(phase=phase, value=1.0, attempts=1, elapsed_seconds=0.0)


class PassingSystem:
    def __init__(self, injector: FlagdFaultInjector):
        self.injector = injector
        self.received = None

    def run(
        self,
        alert: Alert,
        *,
        incident_id: str,
        config_id: Topology,
        tool_budget: int,
        expected_action: dict | None,
    ) -> SystemRun:
        self.received = (alert, incident_id, config_id, tool_budget, expected_action)
        document = read_document(self.injector.config_path)
        updated, _changed = set_variant(document, "paymentFailure", "off")
        write_document(self.injector.config_path, updated)
        action = {
            "action": "set_flag",
            "args": {"flag": "paymentFailure", "variant": "off"},
        }
        return {
            "root_cause_services": ["payment"],
            "proposed_action": action,
            "executed_actions": [action],
            "tool_calls": 4,
            "tokens": 8120,
            "usd": 0.0031,
            "verified_recovery": True,
            "rolled_back": False,
            "model_ids": {"solo": "test-model"},
            "playbook_hash": None,
        }


class FailingSystem:
    def run(self, _alert: Alert, **_kwargs) -> SystemRun:
        raise RuntimeError("model unavailable")


class RecoveryClaimingSystem(PassingSystem):
    """Claim recovery without changing the real flagd state."""

    def run(
        self,
        alert: Alert,
        *,
        incident_id: str,
        config_id: Topology,
        tool_budget: int,
        expected_action: dict | None,
    ) -> SystemRun:
        self.received = (alert, incident_id, config_id, tool_budget, expected_action)
        action = {
            "action": "set_flag",
            "args": {"flag": "paymentFailure", "variant": "off"},
        }
        return {
            "root_cause_services": ["payment"],
            "proposed_action": action,
            "executed_actions": [action],
            "tool_calls": 4,
            "tokens": 8120,
            "usd": 0.0031,
            "verified_recovery": True,
            "rolled_back": False,
            "model_ids": {"solo": "test-model"},
            "playbook_hash": None,
        }


def test_runner_executes_scores_and_persists_one_record(tmp_path: Path):
    injector = _injector(tmp_path)
    system = PassingSystem(injector)
    times = iter([10.0, 10.25])
    runner = EvaluationRunner(
        injector=injector,
        poller=ObservingPoller(injector),  # type: ignore[arg-type]
        system=system,
        artifacts_dir=tmp_path / "runs",
        clock=lambda: next(times),
        id_factory=lambda: "run-001",
    )

    result = runner.run(_case(), _fault(), "c1_single")

    assert result.score.passed
    assert result.record["latency_ms"] == 250
    assert result.record["correct_root_cause"]
    assert result.record["correct_action_proposed"]
    assert result.record["system_reported_recovery"]
    assert result.record["independently_verified_recovery"]
    assert result.record["cleanup_verified"]
    assert result.record["verified_recovery"]
    assert result.record["within_tool_budget"]
    assert result.record["passed"]
    assert result.record["failure_reasons"] == []
    assert result.artifact_path == tmp_path / "runs" / "run-001.json"
    assert load_run_record(result.artifact_path) == result.record
    assert system.received == (
        _case()["alert"],
        "eval-run-001",
        "c1_single",
        5,
        _case()["expected"]["correct_action"],
    )
    assert injector.current_variant("paymentFailure") == "off"

    with pytest.raises(FileExistsError):
        write_run_record(result.artifact_path, result.record)


def test_runner_persists_failure_and_still_cleans_up_fault(tmp_path: Path):
    injector = _injector(tmp_path)
    times = iter([4.0, 4.125])
    runner = EvaluationRunner(
        injector=injector,
        poller=ObservingPoller(injector),  # type: ignore[arg-type]
        system=FailingSystem(),
        artifacts_dir=tmp_path / "runs",
        clock=lambda: next(times),
        id_factory=lambda: "run-failed",
    )

    result = runner.run(_case(), _fault(), "c1_single")

    assert not result.score.passed
    assert result.record["error"] == "RuntimeError: model unavailable"
    assert result.record["latency_ms"] == 125
    assert result.record["tool_calls"] == 0
    assert not result.record["passed"]
    assert result.record["failure_reasons"]
    assert result.artifact_path.is_file()
    assert injector.current_variant("paymentFailure") == "off"


def test_run_record_validator_rejects_contract_drift(tmp_path: Path):
    injector = _injector(tmp_path)
    system = PassingSystem(injector)
    runner = EvaluationRunner(
        injector=injector,
        poller=ObservingPoller(injector),  # type: ignore[arg-type]
        system=system,
        artifacts_dir=tmp_path / "runs",
        id_factory=lambda: "valid",
    )
    record = dict(runner.run(_case(), _fault(), "c1_single").record)
    record.pop("tokens")
    record["surprise"] = True

    problems = validate_run_record(record)

    assert any("missing fields ['tokens']" in problem for problem in problems)
    assert any("unknown fields ['surprise']" in problem for problem in problems)


def test_runner_rejects_a_recovery_claim_not_observed_before_cleanup(tmp_path: Path):
    injector = _injector(tmp_path)
    runner = EvaluationRunner(
        injector=injector,
        poller=ObservingPoller(injector),  # type: ignore[arg-type]
        system=RecoveryClaimingSystem(injector),
        artifacts_dir=tmp_path / "runs",
        id_factory=lambda: "false-recovery",
    )

    result = runner.run(_case(), _fault(), "c1_single")

    assert not result.score.passed
    assert result.record["system_reported_recovery"]
    assert not result.record["independently_verified_recovery"]
    assert result.record["cleanup_verified"]
    assert not result.record["verified_recovery"]
    assert "system recovery claim was not independently observed" in result.score.reasons
    assert "SignalTimeout" in result.record["error"]
    assert injector.current_variant("paymentFailure") == "off"
