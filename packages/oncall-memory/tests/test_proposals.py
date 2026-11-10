"""The proposal log: append only, a reason required to refuse, a person required to decide."""

from __future__ import annotations

import json

import pytest
from oncall_memory.postmortems import APPLICABLE_STATUS, Lesson, Postmortem
from oncall_memory.proposals import ProposalLog, ProposalLogError
from oncall_memory.store import LessonStore


def candidate(lesson_id: str = "les-abc12345", **overrides) -> Lesson:
    fields = {
        "lesson_id": lesson_id,
        "statement": "When checkout errors spike, read the flag baseline before the service.",
        "status": "inferred",
        "service": "payment",
        "preconditions": {"service": "payment", "symptom": "checkout error rate"},
        "derived_from": "alert-c982ca227a2d",
        "evidence_refs": ("run alert-c982ca227a2d", "recovery held"),
        "reason": "",
    }
    fields.update(overrides)
    return Lesson(**fields)


@pytest.fixture
def log(tmp_path) -> ProposalLog:
    return ProposalLog(tmp_path / "lessons.jsonl")


def test_a_missing_log_is_empty_not_an_error(log) -> None:
    assert log.records() == []
    assert log.pending() == []
    assert log.approved() == []


def test_a_proposal_is_pending_and_not_approved(log) -> None:
    log.propose(candidate())

    assert [row["id"] for row in log.pending()] == ["les-abc12345"]
    assert log.approved() == []
    assert log.pending()[0]["status"] == "inferred"


def test_approving_moves_it_into_the_playbook(log) -> None:
    log.propose(candidate())
    log.approve("les-abc12345", by="disen")

    assert log.pending() == []
    assert [row["id"] for row in log.approved()] == ["les-abc12345"]
    assert log.approved()[0]["status"] == APPLICABLE_STATUS


def test_approval_appends_and_does_not_rewrite_the_proposal(log) -> None:
    """`docs/memory-policy.md`: append a superseding record, never edit in place,
    because "we believed X at 10:04" is itself evidence. A reviewer who later
    asks what they were shown has to have an answer."""
    log.propose(candidate())
    log.approve("les-abc12345", by="disen")

    events = log.history("les-abc12345")

    assert [row["event"] for row in events] == ["proposed", "approved"]
    assert events[0]["lesson"]["status"] == "inferred"
    assert events[1]["by"] == "disen"


def test_the_decision_carries_the_lesson_that_was_reviewed(log) -> None:
    """The decision names what was decided on. Without it, an edit upstream
    would silently change what a person approved."""
    log.propose(candidate())
    log.approve("les-abc12345", by="disen")

    proposed, decided = log.history("les-abc12345")

    assert decided["lesson"]["statement"] == proposed["lesson"]["statement"]


def test_rejecting_requires_a_reason(log) -> None:
    """A refusal with no reason is indistinguishable from an oversight, and the
    guide wants one rejected lesson and its reason as a deliverable."""
    log.propose(candidate())

    with pytest.raises(ProposalLogError, match="needs a reason"):
        log.reject("les-abc12345", by="disen", reason="   ")

    assert log.pending(), "a refused refusal must leave the proposal pending"


def test_a_rejection_keeps_the_reason_with_the_lesson(log) -> None:
    log.propose(candidate())
    log.reject("les-abc12345", by="disen", reason="no precondition separates this from 0002")

    refused = log.rejected()

    assert [row["id"] for row in refused] == ["les-abc12345"]
    assert "separates this from 0002" in refused[0]["reason"]
    assert log.approved() == []


def test_a_decision_needs_an_approver(log) -> None:
    log.propose(candidate())

    with pytest.raises(ProposalLogError, match="needs an approver"):
        log.approve("les-abc12345", by="  ")


def test_a_lesson_cannot_be_decided_twice(log) -> None:
    log.propose(candidate())
    log.approve("les-abc12345", by="disen")

    with pytest.raises(ProposalLogError, match="already approved"):
        log.approve("les-abc12345", by="someone-else")
    with pytest.raises(ProposalLogError, match="already approved"):
        log.reject("les-abc12345", by="someone-else", reason="changed my mind")


def test_deciding_on_a_lesson_that_was_never_proposed_is_refused(log) -> None:
    with pytest.raises(ProposalLogError, match="no proposal"):
        log.approve("les-never", by="disen")


def test_the_same_proposal_cannot_be_recorded_twice(log) -> None:
    log.propose(candidate())

    with pytest.raises(ProposalLogError, match="already in the log"):
        log.propose(candidate())


def test_an_already_approved_lesson_cannot_be_proposed(log) -> None:
    """`propose` is the only way into the log, so accepting an `approved` lesson
    here would be a path that reaches the playbook with no person in it."""
    with pytest.raises(ProposalLogError, match="must be 'inferred'"):
        log.propose(candidate(status=APPLICABLE_STATUS))


def test_a_malformed_line_raises_and_names_the_line(log) -> None:
    """Skipping it would turn an unreadable log into "nothing to review", which
    is this project's recurring failure in its exact shape."""
    log.propose(candidate())
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")

    with pytest.raises(ProposalLogError, match=r":2 is not readable JSON"):
        log.pending()


def test_a_well_formed_line_that_is_not_an_event_raises(log) -> None:
    log.path.write_text('{"hello": "world"}\n', encoding="utf-8")

    with pytest.raises(ProposalLogError, match="not a proposal-log event"):
        log.pending()


def test_blank_lines_are_tolerated(log) -> None:
    log.propose(candidate())
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n")

    assert len(log.pending()) == 1


def test_the_log_is_one_json_object_per_line(log) -> None:
    log.propose(candidate())
    log.propose(candidate("les-second00"))
    log.approve("les-second00", by="disen")

    lines = log.path.read_text(encoding="utf-8").strip().splitlines()

    assert len(lines) == 3
    for line in lines:
        assert isinstance(json.loads(line), dict)


# ---- the store merge


def test_an_approved_proposal_joins_the_playbook(log) -> None:
    log.propose(candidate())
    log.approve("les-abc12345", by="disen")

    store = LessonStore((), log)

    assert [row["id"] for row in store.search()] == ["les-abc12345"]


def test_a_pending_proposal_never_reaches_search(log) -> None:
    """The containment that matters. `handoffs.partition_lessons` reads
    `status = lesson.get("status") or "approved"` and drops only `rejected` and
    `pending`, so an `inferred` lesson that reached it would be KEPT and shown
    as reviewed guidance. The store is what stops it getting there."""
    log.propose(candidate())

    store = LessonStore((), log)

    assert store.search() == []
    assert store.rejected() == []


def test_a_rejected_proposal_never_reaches_search(log) -> None:
    log.propose(candidate())
    log.reject("les-abc12345", by="disen", reason="too general")

    store = LessonStore((), log)

    assert store.search() == []


def test_search_still_refuses_to_serve_any_other_status(log) -> None:
    log.propose(candidate())

    with pytest.raises(ValueError, match="serves 'approved' lessons only"):
        LessonStore((), log).search(status="inferred")


def test_approving_a_lesson_changes_the_playbook_hash(log) -> None:
    """`playbook_hash` answers "did these two runs apply the same guidance?", so
    approving must move it and proposing must not."""
    empty = LessonStore((), log).playbook_hash
    log.propose(candidate())
    proposed = LessonStore((), log).playbook_hash
    log.approve("les-abc12345", by="disen")
    approved = LessonStore((), log).playbook_hash

    assert proposed == empty, "an unreviewed proposal is not guidance"
    assert approved != empty


def test_a_store_built_without_a_log_is_exactly_the_committed_corpus(log) -> None:
    """What a held-out run wants: the policy freezes the playbook and writes no
    new lessons during one."""
    log.propose(candidate())
    log.approve("les-abc12345", by="disen")

    assert LessonStore(()).search() == []
    assert LessonStore(()).playbook_hash == LessonStore((), None).playbook_hash


def test_a_proposal_may_not_shadow_a_committed_lesson(log) -> None:
    """Including a committed REJECTED one -- that is how a refused lesson would
    come back as guidance through a side door."""
    seeded = Postmortem(
        incident_id="inc-1",
        title="t",
        service="payment",
        symptom="s",
        path="p",
        body="b",
        lessons=(candidate(status="rejected", reason="refused once already"),),
    )
    log.propose(candidate())
    log.approve("les-abc12345", by="disen")

    with pytest.raises(ValueError, match="both the committed corpus and the proposal log"):
        LessonStore((seeded,), log)
