import json
from pathlib import Path

import pytest
from oncall_eval.execution import EvaluationEvent
from oncall_eval.harness import main
from oncall_eval.live_lock import live_evaluation_lock

ROOT = Path(__file__).resolve().parents[3]


def test_mock_cli_replays_development_fixture_without_live_dependencies(capsys):
    result = main(
        [
            "--mock",
            "--cases",
            str(ROOT / "eval" / "dev"),
            "--fixtures",
            str(ROOT / "eval" / "fixtures"),
            "--manifest",
            str(ROOT / "eval" / "faults" / "manifest.yaml"),
            "--group-by",
            "complexity",
            "--fail-under",
            "1.0",
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "no model or live fault injection" in output
    assert "dev-001: PASS" in output
    assert "single: 1/1" in output


def test_mock_cli_fails_when_a_valid_case_has_no_fixture(tmp_path: Path, capsys):
    cases = tmp_path / "cases"
    cases.mkdir()
    source = ROOT / "eval" / "dev" / "001-payment-failure.json"
    cases.joinpath(source.name).write_bytes(source.read_bytes())

    result = main(
        [
            "--mock",
            "--cases",
            str(cases),
            "--fixtures",
            str(tmp_path / "empty"),
            "--manifest",
            str(ROOT / "eval" / "faults" / "manifest.yaml"),
            "--fail-under",
            "0.0",
        ]
    )

    assert result == 1
    assert "no fixture" in capsys.readouterr().out


def test_mock_cli_fails_on_run_record_contract_drift(tmp_path: Path, capsys):
    cases = tmp_path / "cases"
    fixtures = tmp_path / "fixtures"
    cases.mkdir()
    fixtures.mkdir()
    source = ROOT / "eval" / "dev" / "001-payment-failure.json"
    cases.joinpath(source.name).write_bytes(source.read_bytes())
    fixture = json.loads((ROOT / "eval" / "fixtures" / "dev-001.runrecord.json").read_text())
    fixture.pop("tokens")
    fixtures.joinpath("dev-001.runrecord.json").write_text(json.dumps(fixture))

    result = main(
        [
            "--mock",
            "--cases",
            str(cases),
            "--fixtures",
            str(fixtures),
            "--manifest",
            str(ROOT / "eval" / "faults" / "manifest.yaml"),
            "--fail-under",
            "0.0",
        ]
    )

    assert result == 1
    assert "tokens" in capsys.readouterr().out


def test_live_cli_requires_explicit_fault_injection_acknowledgement(capsys):
    result = main(
        [
            "--live",
            "--cases",
            str(ROOT / "eval" / "dev"),
            "--fail-under",
            "0.6",
        ]
    )

    assert result == 2
    assert "--yes-inject-faults" in capsys.readouterr().out


def test_cli_requires_explicit_acknowledgement_for_heldout_path():
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "--mock",
                "--cases",
                str(ROOT / "eval" / "heldout"),
                "--fail-under",
                "0.6",
            ]
        )

    assert caught.value.code == 2


def test_live_cli_refuses_when_another_evaluation_holds_the_lock(tmp_path: Path, capsys):
    lock = tmp_path / "live-evaluation.lock"

    with live_evaluation_lock(lock):
        result = main(
            [
                "--live",
                "--yes-inject-faults",
                "--cases",
                str(ROOT / "eval" / "dev"),
                "--manifest",
                str(ROOT / "eval" / "faults" / "manifest.yaml"),
                "--lock-file",
                str(lock),
                "--fail-under",
                "1.0",
            ]
        )

    assert result == 2
    assert "another live evaluation" in capsys.readouterr().out


def test_preflight_mode_requires_no_fault_acknowledgement(tmp_path: Path, monkeypatch, capsys):
    called = []

    def fake_execute(request, settings, emit):
        called.append(request)
        emit(EvaluationEvent("complete", "preflight PASS: stub"))
        return type("Result", (), {"passed": True})()

    monkeypatch.setattr("oncall_eval.harness.execute_evaluation", fake_execute)

    result = main(
        [
            "--preflight",
            "--cases",
            str(ROOT / "eval" / "dev"),
            "--manifest",
            str(ROOT / "eval" / "faults" / "manifest.yaml"),
            "--lock-file",
            str(tmp_path / "preflight.lock"),
            "--fail-under",
            "1.0",
        ]
    )

    assert result == 0
    assert called[0].kind == "preflight"
    assert called[0].config_id == "c1_single"
    assert "preflight PASS" in capsys.readouterr().out
