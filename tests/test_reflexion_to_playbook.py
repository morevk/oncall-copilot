"""Reflexion to playbook, across the seam: what a person approved is what the agent gets.

This file lives in the root test tree because it imports BOTH `oncall_memory`
and `oncall_graph`, and `test_dependency_direction` scans whole package
directories including their tests.

It proves two things.

ONE: a lesson the reflexion loop proposed, and a person approved, is applicable
to the incident that justified it and is dropped for an incident that does not
match. That is the whole loop -- propose, approve, apply -- with P2's real
`partition_lessons` doing the filtering, not a stand-in.

TWO: an unreviewed lesson is refused TWICE, by two owners' code independently.
`partition_lessons` keeps only an explicit `approved`, and `LessonStore.search`
serves only `approved` and raises on anything else. Either one alone would
contain a proposal; the point of testing both is that neither is load-bearing
on its own.

That second property was a warning when this file was written. `handoffs.py`
read `status = lesson.get("status") or "approved"` and dropped only `rejected`
and `pending`, so `inferred` -- the status every proposal carries -- fell
through to the kept list, and the store was the only thing containing it. The
hazard became reachable the moment reflexion existed, which is why it was
pinned here rather than described. #36 closed it. The two tests that asserted
the fail-open are inverted below, and they now assert that the filter SEES an
unreviewed lesson and refuses it.
"""

from __future__ import annotations

import pytest
from oncall_graph.handoffs import load_lessons, partition_lessons
from oncall_memory import Lesson, LessonStore, ProposalLog, propose

#: The alert the lab actually posts for `payment-failure`.
ALERT = "checkout error rate above 5% for five minutes"
SERVICE = "payment"

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
    "independently_verified_recovery": True,
    "cleanup_verified": True,
    "verified_recovery": True,
    "harmful_actions": 0,
    "rolled_back": False,
    "within_tool_budget": True,
    "passed": True,
    "failure_reasons": [],
    "model_ids": {"solo": "openai/gpt-4o-mini"},
    "playbook_hash": "seed",
    "error": None,
    "source_incident_id": "alert-c982ca227a2d",
}


@pytest.fixture
def log(tmp_path) -> ProposalLog:
    return ProposalLog(tmp_path / "lessons.jsonl")


def test_an_approved_proposal_reaches_the_agent_for_the_case_it_came_from(log) -> None:
    lesson = propose(RUN, alert=ALERT, service=SERVICE)
    log.propose(lesson)
    log.approve(lesson.lesson_id, by="disen")

    kept, dropped = load_lessons(LessonStore((), log), service=SERVICE, symptom=ALERT)

    assert [row["id"] for row in kept] == [lesson.lesson_id]
    assert dropped == []


def test_the_same_lesson_is_dropped_for_a_different_service(log) -> None:
    lesson = propose(RUN, alert=ALERT, service=SERVICE)
    log.propose(lesson)
    log.approve(lesson.lesson_id, by="disen")

    kept, dropped = load_lessons(LessonStore((), log), service="recommendation", symptom=ALERT)

    assert kept == []
    assert [row["id"] for row in dropped] == [lesson.lesson_id]


def test_the_same_lesson_is_dropped_for_a_different_symptom(log) -> None:
    """The precondition reflexion wrote is the alert it was proposed from, and
    `precondition_matches` tests containment both ways -- so an unrelated
    symptom does not match."""
    lesson = propose(RUN, alert=ALERT, service=SERVICE)
    log.propose(lesson)
    log.approve(lesson.lesson_id, by="disen")

    kept, dropped = load_lessons(
        LessonStore((), log), service=SERVICE, symptom="container is unhealthy"
    )

    assert kept == []
    assert [row["id"] for row in dropped] == [lesson.lesson_id]


def test_a_pending_proposal_never_reaches_the_agent(log) -> None:
    lesson = propose(RUN, alert=ALERT, service=SERVICE)
    log.propose(lesson)

    kept, dropped = load_lessons(LessonStore((), log), service=SERVICE, symptom=ALERT)

    assert kept == []
    assert dropped == []


def test_a_rejected_proposal_never_reaches_the_agent(log) -> None:
    lesson = propose(RUN, alert=ALERT, service=SERVICE)
    log.propose(lesson)
    log.reject(lesson.lesson_id, by="disen", reason="no separating precondition")

    kept, dropped = load_lessons(LessonStore((), log), service=SERVICE, symptom=ALERT)

    assert kept == []
    assert dropped == []


def test_partition_lessons_drops_an_inferred_lesson_that_reaches_it() -> None:
    """THE HAZARD, now closed, and pinned from the other side.

    This asserted the opposite until #36: `inferred` was on neither of
    `partition_lessons`' drop lists, so a proposal that reached the filter was
    handed to the model as reviewed guidance, and the store was all that stood
    in the way. The filter keeps only an explicit `approved` now.

    The assertion is that the lesson appears in DROPPED, not merely that `kept`
    is empty. An empty `kept` is also what an empty input produces, and that
    would pass while proving nothing.
    """
    unreviewed = Lesson(
        lesson_id="les-unreviewed",
        statement="a sentence no person has read",
        status="inferred",
        service=SERVICE,
        preconditions={"service": SERVICE, "symptom": ALERT},
        derived_from="alert-c982ca227a2d",
    ).as_dict()

    kept, dropped = partition_lessons([unreviewed], service=SERVICE, symptom=ALERT)

    assert kept == []
    assert [row["id"] for row in dropped] == ["les-unreviewed"]


def test_neither_the_store_nor_the_filter_is_load_bearing_alone(log) -> None:
    """Both halves of the containment in one place, each proved separately.

    Before #36 this test recorded that the filter WOULD have kept the proposal
    and that the store was the only reason it never saw one. Both refuse now,
    which is the property worth keeping: remove either and an unreviewed
    sentence still does not reach the model.
    """
    lesson = propose(RUN, alert=ALERT, service=SERVICE)
    log.propose(lesson)
    store = LessonStore((), log)

    # Half one: hand the filter the proposal directly, bypassing the store.
    straight_kept, straight_dropped = partition_lessons(
        [lesson.as_dict()], service=SERVICE, symptom=ALERT
    )
    # Half two: ask the store for it, which is the path the graph actually takes.
    through_the_store, _ = load_lessons(store, service=SERVICE, symptom=ALERT)

    assert straight_kept == []
    assert [row["id"] for row in straight_dropped] == [lesson.lesson_id]
    assert through_the_store == []


def test_the_committed_corpus_and_an_approved_proposal_are_indistinguishable(log) -> None:
    """Provenance lives in `derived_from`, not in which file a lesson came from.
    The graph receives one list and does not know, which is what keeps the seed
    corpus from being a special case."""
    lesson = propose(RUN, alert=ALERT, service=SERVICE)
    log.propose(lesson)
    log.approve(lesson.lesson_id, by="disen")

    merged = LessonStore.from_postmortems(proposals=log)
    kept, _ = load_lessons(merged, service=SERVICE, symptom=ALERT)

    ids = [row["id"] for row in kept]

    assert lesson.lesson_id in ids
    assert "les-read-the-flag-baseline-first" in ids
    assert all(row["status"] == "approved" for row in kept)
