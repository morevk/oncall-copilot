"""The execution layer is what both the CLI and the Control Center call."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from oncall_eval.execution import (
    EvaluationEvent,
    EvaluationRefused,
    EvaluationRequest,
    EvaluationSettings,
    execute_evaluation,
)
from oncall_eval.harness import main
from oncall_eval.http_client import IncidentApiClient
from oncall_eval.injector import FaultInjectionError, FlagdFaultInjector
from oncall_eval.signals import PrometheusClient

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "eval" / "faults" / "manifest.yaml"
CASES = ROOT / "eval" / "dev"


def _flag(path: Path, variant: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "flags": {
                    "paymentFailure": {
                        "defaultVariant": variant,
                        "variants": {"off": 0, "100%": 1},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _settings(tmp_path: Path, **overrides) -> EvaluationSettings:
    values = {
        "cases_dir": CASES,
        "manifest_path": MANIFEST,
        "flagd_config": tmp_path / "demo.flagd.json",
        "prometheus_url": "http://prometheus.test",
        "api_url": "http://incident.test",
        "artifacts_dir": tmp_path / "runs",
        "lock_file": tmp_path / "locks" / "live-evaluation.lock",
        "fail_under": 1.0,
    }
    values.update(overrides)
    return EvaluationSettings(**values)


def _quiet_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(IncidentApiClient, "preflight", lambda self, config_id: None)
    monkeypatch.setattr(PrometheusClient, "sample", lambda self, query: 0.0)


def _events():
    seen: list[EvaluationEvent] = []

    def emit(event: EvaluationEvent) -> None:
        seen.append(event)

    return seen, emit


def test_unknown_topology_does_not_touch_the_lab(tmp_path: Path):
    flag = tmp_path / "missing-flags.json"
    lock = tmp_path / "locks" / "live.lock"
    seen, emit = _events()

    with pytest.raises(ValueError, match="unknown topology"):
        execute_evaluation(
            EvaluationRequest(kind="preflight", config_id="c9_missing"),
            _settings(tmp_path, flagd_config=flag, lock_file=lock, cases_dir=tmp_path / "absent"),
            emit,
        )

    assert seen == []
    assert not flag.exists()
    assert not lock.exists()


def test_unknown_case_id_is_rejected_before_any_flag_write(tmp_path: Path, monkeypatch):
    flag = _flag(tmp_path / "demo.flagd.json", "off")
    before = flag.read_bytes()
    injected: list[str] = []
    monkeypatch.setattr(
        FlagdFaultInjector,
        "injected",
        lambda self, fault: injected.append(fault["fault_id"]),
    )

    with pytest.raises(ValueError, match="unknown case ids: missing-case"):
        execute_evaluation(
            EvaluationRequest(
                kind="deterministic", config_id="c1_single", case_ids=("missing-case",)
            ),
            _settings(tmp_path, flagd_config=flag),
            lambda _event: None,
        )

    assert injected == []
    assert flag.read_bytes() == before
    assert not (tmp_path / "locks" / "live-evaluation.lock").exists()


def test_preflight_checks_the_baseline_and_does_not_inject(tmp_path: Path, monkeypatch):
    flag = _flag(tmp_path / "demo.flagd.json", "off")
    before = flag.read_bytes()
    _quiet_dependencies(monkeypatch)
    injected: list[str] = []
    monkeypatch.setattr(
        FlagdFaultInjector,
        "injected",
        lambda self, fault: injected.append(fault["fault_id"]),
    )
    seen, emit = _events()

    result = execute_evaluation(
        EvaluationRequest(kind="preflight", config_id="c1_single", case_ids=("dev-001",)),
        _settings(tmp_path, flagd_config=flag),
        emit,
    )

    assert result.passed is True
    assert result.run_ids == ()
    assert injected == []
    assert flag.read_bytes() == before
    assert [event.phase for event in seen] == ["validating", "preflight", "complete"]
    assert seen[-1].message.startswith("preflight PASS: c1_single")


def test_dirty_baseline_refuses_a_live_run_before_injection(tmp_path: Path, monkeypatch):
    flag = _flag(tmp_path / "demo.flagd.json", "100%")
    before = flag.read_bytes()
    _quiet_dependencies(monkeypatch)
    injected: list[str] = []
    monkeypatch.setattr(
        FlagdFaultInjector,
        "injected",
        lambda self, fault: injected.append(fault["fault_id"]),
    )

    with pytest.raises(EvaluationRefused, match="baseline"):
        execute_evaluation(
            EvaluationRequest(kind="deterministic", config_id="c1_single", case_ids=("dev-001",)),
            _settings(tmp_path, flagd_config=flag),
            lambda _event: None,
        )

    assert injected == []
    assert flag.read_bytes() == before


def test_preflight_reports_a_dirty_baseline_as_its_own_error(tmp_path: Path, monkeypatch):
    flag = _flag(tmp_path / "demo.flagd.json", "100%")
    _quiet_dependencies(monkeypatch)

    with pytest.raises(FaultInjectionError, match="baseline"):
        execute_evaluation(
            EvaluationRequest(kind="preflight", config_id="c1_single"),
            _settings(tmp_path, flagd_config=flag),
            lambda _event: None,
        )


def test_a_failure_during_the_run_is_not_relabelled_as_preflight(tmp_path: Path, monkeypatch):
    flag = _flag(tmp_path / "demo.flagd.json", "off")
    _quiet_dependencies(monkeypatch)

    def explode(self, case, fault, config_id):
        raise FaultInjectionError("injector failed during the run")

    monkeypatch.setattr("oncall_eval.execution.EvaluationRunner.run", explode)

    with pytest.raises(FaultInjectionError, match="during the run"):
        execute_evaluation(
            EvaluationRequest(kind="deterministic", config_id="c1_single", case_ids=("dev-001",)),
            _settings(tmp_path, flagd_config=flag),
            lambda _event: None,
        )


def test_deterministic_run_emits_scoring_without_finer_phases(tmp_path: Path, monkeypatch):
    flag = _flag(tmp_path / "demo.flagd.json", "off")
    _quiet_dependencies(monkeypatch)

    def fake_run(self, case, fault, config_id):
        return SimpleNamespace(
            record={"run_id": "run-123"},
            score=SimpleNamespace(passed=True, reasons=()),
            artifact_path=tmp_path / "run-123.json",
        )

    monkeypatch.setattr("oncall_eval.execution.EvaluationRunner.run", fake_run)
    seen, emit = _events()

    result = execute_evaluation(
        EvaluationRequest(kind="deterministic", config_id="c1_single", case_ids=("dev-001",)),
        _settings(tmp_path, flagd_config=flag, group_by="complexity"),
        emit,
    )

    assert result.passed is True
    assert result.run_ids == ("run-123",)
    phases = [event.phase for event in seen]
    assert phases[0:3] == ["validating", "preflight", "injecting"]
    assert phases[-1] == "complete"
    assert set(phases) <= {"validating", "preflight", "injecting", "scoring", "complete"}
    assert "by complexity:" in [event.message for event in seen]
    assert "pass rate: 1/1 = 1.000; required 1.000" in seen[-1].message


def test_cli_hides_progress_phases_and_keeps_the_historical_lines(
    tmp_path: Path, monkeypatch, capsys
):
    flag = _flag(tmp_path / "demo.flagd.json", "off")
    _quiet_dependencies(monkeypatch)

    def fake_run(self, case, fault, config_id):
        return SimpleNamespace(
            record={"run_id": "run-123"},
            score=SimpleNamespace(passed=False, reasons=("recovery was not observed",)),
            artifact_path=tmp_path / "run-123.json",
        )

    monkeypatch.setattr("oncall_eval.execution.EvaluationRunner.run", fake_run)

    result = main(
        [
            "--live",
            "--yes-inject-faults",
            "--cases",
            str(CASES),
            "--manifest",
            str(MANIFEST),
            "--flagd-config",
            str(flag),
            "--lock-file",
            str(tmp_path / "locks" / "live.lock"),
            "--fail-under",
            "1.0",
            "--group-by",
            "complexity",
        ]
    )

    output = capsys.readouterr().out
    assert result == 1
    assert "validated" not in output
    assert "checking the incident API" not in output
    assert "live mode: running 1 case(s) against c1_single" in output
    assert "dev-001: FAIL — recovery was not observed" in output
    assert "single: 0/1" in output
    assert "pass rate: 0/1 = 0.000; required 1.000" in output


def test_cli_preflight_prints_only_the_pass_line(tmp_path: Path, monkeypatch, capsys):
    flag = _flag(tmp_path / "demo.flagd.json", "off")
    _quiet_dependencies(monkeypatch)

    result = main(
        [
            "--preflight",
            "--cases",
            str(CASES),
            "--manifest",
            str(MANIFEST),
            "--flagd-config",
            str(flag),
            "--lock-file",
            str(tmp_path / "locks" / "preflight.lock"),
            "--fail-under",
            "1.0",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert output.strip().startswith("preflight PASS:")
    assert "validated" not in output
