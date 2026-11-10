"""`python -m oncall_memory.review`: the human gate, and what it refuses to do."""

from __future__ import annotations

import json

import pytest
from oncall_memory.proposals import ProposalLog
from oncall_memory.review import build_parser, main

RUN = {
    "run_id": "run-0001",
    "case_id": "case-payment-01",
    "fault_id": "payment-failure",
    "config_id": "c1_single",
    "complexity": "single",
    "tool_calls": 7,
    "tokens": 4200,
    "usd": 0.012,
    "latency_ms": 31000,
    "correct_root_cause": True,
    "root_cause_rank": 1,
    "correct_action_proposed": True,
    "system_reported_recovery": True,
    "independently_verified_recovery": False,
    "cleanup_verified": True,
    "verified_recovery": False,
    "harmful_actions": 0,
    "rolled_back": False,
    "within_tool_budget": True,
    "passed": False,
    "failure_reasons": ["recovery not independently verified"],
    "model_ids": {"solo": "openai/gpt-4o-mini"},
    "playbook_hash": "abc123",
    "error": None,
    "source_incident_id": "alert-c982ca227a2d",
}
ALERT = "checkout error rate above 5% for five minutes"


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "lessons.jsonl"


@pytest.fixture
def run_file(tmp_path):
    path = tmp_path / "run.json"
    path.write_text(json.dumps(RUN), encoding="utf-8")
    return path


def run_cli(log_path, *argv) -> int:
    return main(["--log", str(log_path), *argv])


def propose_one(log_path, run_file) -> str:
    assert (
        run_cli(
            log_path, "propose", "--run", str(run_file), "--alert", ALERT, "--service", "payment"
        )
        == 0
    )
    return ProposalLog(log_path).pending()[0]["id"]


def test_an_empty_log_lists_nothing_and_succeeds(log_path, capsys) -> None:
    assert run_cli(log_path, "list") == 0
    assert "nothing pending" in capsys.readouterr().out


def test_listing_is_the_default_command(log_path, capsys) -> None:
    assert main(["--log", str(log_path)]) == 0
    assert "nothing pending" in capsys.readouterr().out


def test_proposing_records_an_inferred_lesson_and_says_so(log_path, run_file, capsys) -> None:
    propose_one(log_path, run_file)
    out = capsys.readouterr().out

    assert "NOT applied by the agent until approved" in out
    assert "Nothing applies it until a person approves it" in out
    assert ProposalLog(log_path).pending()[0]["status"] == "inferred"


def test_the_proposal_from_an_unconfirmed_recovery_says_what_to_distrust(
    log_path, run_file, capsys
) -> None:
    """The run in this file claimed recovery telemetry never confirmed, which is
    the case worth a lesson."""
    propose_one(log_path, run_file)

    assert "did not confirm" in capsys.readouterr().out


def test_the_listing_marks_a_pending_lesson_as_inferred(log_path, run_file, capsys) -> None:
    propose_one(log_path, run_file)
    capsys.readouterr()

    run_cli(log_path, "list")

    assert "[inferred]" in capsys.readouterr().out


def test_approving_reports_the_approver_and_the_hash_change(log_path, run_file, capsys) -> None:
    lesson_id = propose_one(log_path, run_file)
    capsys.readouterr()

    assert run_cli(log_path, "approve", lesson_id, "--by", "disen") == 0
    out = capsys.readouterr().out

    assert f"approved {lesson_id} by disen" in out
    assert "playbook_hash has changed" in out
    assert ProposalLog(log_path).pending() == []


def test_rejecting_keeps_the_reason(log_path, run_file, capsys) -> None:
    lesson_id = propose_one(log_path, run_file)
    capsys.readouterr()

    assert (
        run_cli(log_path, "reject", lesson_id, "--by", "disen", "--reason", "too general to act on")
        == 0
    )

    assert "too general to act on" in capsys.readouterr().out
    assert ProposalLog(log_path).rejected()[0]["reason"] == "too general to act on"


def test_the_rejected_listing_shows_refusals_with_their_reasons(log_path, run_file, capsys) -> None:
    """One rejected lesson and its reason is a required deliverable, so there is
    a command that prints exactly that."""
    lesson_id = propose_one(log_path, run_file)
    run_cli(log_path, "reject", lesson_id, "--by", "disen", "--reason", "no separating guard")
    capsys.readouterr()

    assert run_cli(log_path, "rejected") == 0
    out = capsys.readouterr().out

    assert lesson_id in out
    assert "no separating guard" in out


def test_show_prints_every_decision_in_order(log_path, run_file, capsys) -> None:
    lesson_id = propose_one(log_path, run_file)
    run_cli(log_path, "approve", lesson_id, "--by", "disen")
    capsys.readouterr()

    assert run_cli(log_path, "show", lesson_id) == 0
    out = capsys.readouterr().out

    assert out.index("proposed") < out.index("approved by disen")
    assert "alert-c982ca227a2d" in out


def test_rejecting_without_a_reason_cannot_even_be_expressed(log_path) -> None:
    """`--reason` is a required argument, so the refusal happens in argparse
    before anything touches the log."""
    with pytest.raises(SystemExit) as exit_code:
        run_cli(log_path, "reject", "les-whatever")

    assert exit_code.value.code == 2


def test_there_is_no_way_to_approve_a_whole_list(log_path) -> None:
    """The value of this gate is that a person read the sentence. A flag that
    approved an unread list would leave an audit trail saying a person was
    there, which is worse than no gate at all."""
    actions = build_parser().format_help() + build_parser().parse_args(["list"]).__repr__()

    for forbidden in ("--all", "--approve-all", "--yes", "--force"):
        assert forbidden not in actions


def test_a_refusal_prints_as_a_refusal_and_exits_non_zero(log_path, capsys) -> None:
    assert run_cli(log_path, "approve", "les-never", "--by", "disen") == 1

    assert "refused: no proposal" in capsys.readouterr().err


def test_a_missing_run_record_is_reported_not_raised(log_path, tmp_path, capsys) -> None:
    assert (
        run_cli(
            log_path,
            "propose",
            "--run",
            str(tmp_path / "nope.json"),
            "--alert",
            ALERT,
            "--service",
            "payment",
        )
        == 1
    )

    assert "could not read the run record" in capsys.readouterr().err


def test_a_run_record_with_an_unclassified_field_is_refused(log_path, tmp_path, capsys) -> None:
    path = tmp_path / "odd.json"
    path.write_text(json.dumps({**RUN, "root_cause_confidence": 0.9}), encoding="utf-8")

    assert (
        run_cli(log_path, "propose", "--run", str(path), "--alert", ALERT, "--service", "payment")
        == 1
    )

    assert "unclassified run-record field" in capsys.readouterr().err


def test_drafting_with_no_api_key_refuses_rather_than_pretending(
    log_path, run_file, monkeypatch, capsys
) -> None:
    """A caller that asked for a model and silently got the template would
    report a reflexion loop it did not run."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert (
        run_cli(
            log_path,
            "propose",
            "--run",
            str(run_file),
            "--alert",
            ALERT,
            "--service",
            "payment",
            "--draft-with-model",
        )
        == 1
    )

    assert "OPENROUTER_API_KEY is not set" in capsys.readouterr().err
    assert ProposalLog(log_path).records() == []
