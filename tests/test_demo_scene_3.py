"""Demo scene 3, end to end: an old lesson rejected because its preconditions do not match.

This file lives in the root test tree on purpose. It imports BOTH `oncall_memory`
and `oncall_graph`, and `test_dependency_direction` scans whole package
directories including their tests -- so a test of the seam between two packages
cannot live inside either one.

What it proves is the mechanism, not the prose: the store hands over every
approved lesson, and P2's `partition_lessons` is what drops the ones whose
preconditions do not fit the live case. Both halves have to be present for the
scene to show anything.
"""

from __future__ import annotations

import pytest
from oncall_graph.handoffs import load_lessons, runbook_guidance
from oncall_graph.state import new_incident
from oncall_graph.stubs import Shop
from oncall_graph.team_demo import (
    CHECKOUT_ALERT,
    PAYMENT_CHARGE_REQUESTS_ALERT,
    PAYMENT_UNAVAILABLE_ALERT,
)
from oncall_graph.topologies.c2_team import Team
from oncall_memory import APPLICABLE_STATUS, LessonStore

#: The alert the demo actually pages. `eval/faults/manifest.yaml` publishes the
#: checkout symptom for `payment-failure`, and the lab posts exactly this text.
CASE = {"service": "payment", "symptom": "checkout error rate above 5% for five minutes"}


def playbook() -> LessonStore:
    return LessonStore.from_postmortems()


def test_the_matching_lesson_is_kept_and_the_stale_ones_are_dropped() -> None:
    """Exactly one lesson is kept, and exactly these three are refused, in order.

    The pin is exhaustive ON PURPOSE, and it carries a known maintenance cost:
    every new seed postmortem whose lessons do not fit CASE lands in this list,
    and the corpus is now the full 10. Pay that cost rather than weaken the
    assertion: ten refusals and one keep is what the scene has to show.
    A membership check cannot see a lesson that starts being DROPPED when it
    should be kept, and it cannot see the order change -- both are regressions
    this file exists to catch.
    """
    kept, dropped = load_lessons(playbook(), **CASE)

    assert [lesson["id"] for lesson in kept] == ["les-read-the-flag-baseline-first"]
    assert [lesson["id"] for lesson in dropped] == [
        "les-a-multiplier-variant-is-not-a-boolean",
        "les-a-proportional-fault-reads-as-flaky-clients",
        "les-a-soft-limit-never-fails",
        "les-absent-spans-are-not-failing-spans",
        "les-confirm-the-variant-the-page-names",
        "les-health-check-names-a-binary",
        "les-minimal-mode-makes-this-fault-impossible",
        "les-one-service-two-faults-read-the-flag-pair",
        "les-the-delay-variant-names-the-duration",
        "les-the-healthcheck-patch-hides-cache-degradation",
    ]


def test_a_dropped_lesson_carries_the_precondition_that_disqualified_it() -> None:
    """The scene has to be able to say WHY. A dropped list of bare ids shows a
    filter ran; it does not show the reasoning the objective asks us to
    demonstrate."""
    _, dropped = load_lessons(playbook(), **CASE)

    for lesson in dropped:
        preconditions = lesson["preconditions"]
        assert preconditions, f"{lesson['id']} was dropped with nothing to point at"
        # Not "the service differs": a lesson for THIS service with a symptom
        # for another page in the same fault family is also correctly dropped,
        # and the corpus has one. Asserting the service branch specifically
        # would have made that drop look like a bug.
        wrong_service = preconditions.get("service") != CASE["service"]
        symptom = (preconditions.get("symptom") or "").lower()
        hay = CASE["symptom"].lower()
        wrong_symptom = bool(symptom) and symptom not in hay and hay not in symptom
        assert wrong_service or wrong_symptom, (
            f"{lesson['id']} was dropped but its preconditions fit {CASE}"
        )


def test_nothing_unreviewed_or_refused_ever_reaches_the_graph() -> None:
    """The invariant, checked at the seam rather than inside the store.

    The store only hands over approved lessons, and `partition_lessons` keeps
    only an explicit `approved`, so a lesson that arrives with no status is
    dropped rather than shown as guidance.
    """
    kept, dropped = load_lessons(playbook(), **CASE)

    for lesson in kept + dropped:
        assert lesson["status"] == APPLICABLE_STATUS, (
            f"{lesson['id']} is {lesson['status']!r} and reached the graph"
        )


def test_the_rejected_lesson_is_a_deliverable_and_stays_out_of_the_run() -> None:
    """At least one rejected lesson with its reason is required by the project
    guide. Every refusal must be visible to a person and invisible to the agent.

    This iterates rather than unpacking a single refusal. The guide asks for one;
    it does not ask for exactly one, and pinning the count made the second
    refusal -- inc-2026-11-03-01, where the payment charge-path lesson is wrong
    -- look like a broken test instead of a second worked example.
    """
    store = playbook()
    refusals = store.rejected()
    kept, dropped = load_lessons(store, **CASE)
    reached = [lesson["id"] for lesson in kept + dropped]

    assert refusals, "the guide requires a refused lesson, kept with its reason"
    for refused in refusals:
        assert refused["reason"], f"{refused['id']} was refused with no reason recorded"
        assert refused["id"] not in reached, f"{refused['id']} reached the graph"


def test_the_team_runbook_drops_the_stale_lessons_and_says_why() -> None:
    """The node the live team runs, against the real playbook, with no model call.

    The budget is already spent, so the runbook specialist is not asked. The
    filter still runs, and the transcript has to name the precondition.
    """
    team = Team(shop=Shop(), store=playbook(), tool_budget=10)
    state = new_incident(
        alert={
            "status": "firing",
            "labels": {"alertname": "CheckoutErrorRate", "service": "checkout"},
            "annotations": {"summary": CASE["symptom"]},
            "startsAt": "2026-11-08T00:00:00Z",
        },
        hypotheses=[
            {
                "service": "payment",
                "statement": "paymentFailure flag",
                "evidence_refs": ["checkout_error_rate"],
                "confidence": 0.9,
            }
        ],
        tool_budget=10,
        tool_calls=10,
    )
    update = team.runbook(state)
    lines = [item["text"] for item in update["transcript"]]
    result = update["last"]["result"]

    assert [lesson["id"] for lesson in result["lessons"]] == ["les-read-the-flag-baseline-first"]
    assert result["dropped_lessons"] == [
        "les-a-multiplier-variant-is-not-a-boolean",
        "les-a-proportional-fault-reads-as-flaky-clients",
        "les-a-soft-limit-never-fails",
        "les-absent-spans-are-not-failing-spans",
        "les-confirm-the-variant-the-page-names",
        "les-health-check-names-a-binary",
        "les-minimal-mode-makes-this-fault-impossible",
        "les-one-service-two-faults-read-the-flag-pair",
        "les-the-delay-variant-names-the-duration",
        "les-the-healthcheck-patch-hides-cache-degradation",
    ]
    assert (
        "dropped lesson les-a-soft-limit-never-fails: precondition service product-catalog != payment"
        in lines
    )
    assert (
        "dropped lesson les-health-check-names-a-binary: precondition service recommendation != payment"
        in lines
    )
    # The symptom branch of `lesson_drop_line`, which no committed lesson reached
    # until the payment family got a second register. A drop inside the right
    # service is the discrimination this objective is actually about: a service
    # match alone is the easy half. The id is pinned in the list above; this is
    # the SENTENCE, which is the part the scene reads out loud.
    assert (
        "dropped lesson les-confirm-the-variant-the-page-names: precondition symptom "
        "'payment service charge requests' does not match 'checkout error rate above 5% "
        "for five minutes'" in lines
    )
    guidance = runbook_guidance(result["passages"], result["lessons"])
    assert "les-read-the-flag-baseline-first" in guidance
    assert "read the flagd baseline" in guidance
    assert "les-a-soft-limit-never-fails" not in guidance


#: What the playbook can and cannot speak to, per alert the demo actually posts.
#: The alerts are IMPORTED, not restated, because the bug this table exists to
#: catch was a rename: #33 made the payment-charge page the headline, its summary
#: shares no substring with the checkout page, and the corpus quietly kept NOTHING
#: on it. Nothing failed -- the scene just showed three rejections and no guidance,
#: which does not distinguish a working filter from an empty playbook.
#:
#: The focus service is `payment` in every row because `focus_service` reads the
#: hypothesis the diagnosis produced, and the runbook step runs after it.
COVERAGE = {
    "PAYMENT_CHARGE_REQUESTS_ALERT": (PAYMENT_CHARGE_REQUESTS_ALERT, True),
    "CHECKOUT_ALERT": (CHECKOUT_ALERT, True),
    # Was an honest gap until inc-2026-11-03-01 was written up. It is closed by
    # a lesson of its OWN -- `les-absent-spans-are-not-failing-spans` -- and not
    # by widening a payment-failure lesson, which is what the comment here used
    # to warn against. All three payment registers now keep a different lesson.
    "PAYMENT_UNAVAILABLE_ALERT": (PAYMENT_UNAVAILABLE_ALERT, True),
}


@pytest.mark.parametrize("name", sorted(COVERAGE))
def test_every_alert_the_demo_posts_has_a_stated_memory_outcome(name: str) -> None:
    """Coverage of the playbook against the pages the demo really sends.

    A lesson is kept only if its precondition names the register the page is
    written in. Two of these pages are metrics prose and one quotes a flagd
    variant description, so this is the test that notices when an alert is
    reworded and the playbook silently stops matching it.
    """
    alert, should_keep = COVERAGE[name]
    symptom = alert["annotations"]["summary"]

    kept, dropped = load_lessons(playbook(), service="payment", symptom=symptom)

    if should_keep:
        assert kept, (
            f"{name} keeps no lesson: summary {symptom!r} matches no precondition in the "
            "corpus, so scene 3 shows rejections and no guidance on this page"
        )
        for lesson in kept:
            assert lesson["statement"].strip(), f"{name} kept {lesson['id']} with no sentence"
    else:
        assert not kept, (
            f"{name} now keeps {[lesson['id'] for lesson in kept]}. If that is a lesson "
            "written for this fault, set its row in COVERAGE to True."
        )
    assert dropped, f"{name} dropped nothing, so the filter was not exercised at all"


def test_an_empty_playbook_drops_nothing_and_raises_nothing() -> None:
    """A memory-off arm of the comparison, and the shape a fresh clone has."""
    assert load_lessons(None, **CASE) == ([], [])
    assert load_lessons([], **CASE) == ([], [])
