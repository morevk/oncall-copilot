"""Reflexion: propose a candidate lesson from a finished run. Never approve one.

THE MODEL WRITES ONE SENTENCE AND NOTHING ELSE. Every field code can check,
code fills: `status` is forced to `inferred`, `preconditions` are restricted to
the two keys `oncall_graph.briefs.precondition_matches` actually evaluates,
`derived_from` comes from the run's own identity, and `evidence_refs` are
assembled from observed values. A drafter that returns prose cannot change any
of them, and `test_a_drafter_cannot_set_its_own_status` is what keeps that true.

That split is the project's first rule made literal -- the model proposes, code
and people decide -- and it is also the narrow version of a failure this
repository keeps finding. Letting a model write `preconditions` would let it
write a guard that LOOKS checked and is not: `precondition_matches` reads
`service` and `symptom` and silently ignores every other key, so
`{"deploy_in_last_30m": "true"}` would read as a careful lesson and match
everything. `EVALUABLE_PRECONDITIONS` exists because authoring one is refused
rather than ignored, and this module may not route around it.

WHY THIS DOES NOT READ MOST OF THE RUN RECORD, WHICH IS THE IMPORTANT PART.

`RunRecord` is mostly P4's scoring. The evaluator knows the true cause because
it injected the fault, so `fault_id`, `correct_root_cause`, `root_cause_rank`,
`correct_action_proposed`, `harmful_actions`, `passed` and `failure_reasons` are
all answers from an oracle that does not exist outside the harness. A lesson
derived from any of them would be correct because the grader said so, and the
memory-on arm of the experiment would measure `eval/faults/manifest.yaml`
instead of measuring learning. The playbook would look like it had learned,
every lesson in it would be right, and the number it produced would be
meaningless -- the same shape as every other failure in this project.

So `OBSERVABLE_FIELDS` is what reflexion may read: the run's identity, its own
cost, and whether the recovery signal came back. All of it is available after a
real incident, with no oracle anywhere. `ORACLE_FIELDS` is the rest, and the two
sets are asserted to partition `RunRecord` exactly, so a field added later must
be classified by whoever adds it rather than defaulting into the agent's memory.

`verified_recovery` is in the refused set even though it is derivable from two
permitted fields. It is the harness's verdict, and reflexion composing its own
conclusion from `system_reported_recovery` and `independently_verified_recovery`
is the point: the interesting case is where those two disagree, and a module
that read the grade would not have to notice.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from .postmortems import EVALUABLE_PRECONDITIONS, Lesson

#: The only status reflexion may produce. A proposal is not guidance.
PROPOSED_STATUS = "inferred"

#: What reflexion may read: identity, own cost, and whether recovery was
#: observed. Every one of these exists after a real incident.
OBSERVABLE_FIELDS = frozenset(
    {
        "run_id",
        "source_incident_id",
        "config_id",
        "tool_calls",
        "tokens",
        "usd",
        "latency_ms",
        "within_tool_budget",
        "system_reported_recovery",
        "independently_verified_recovery",
        "rolled_back",
        "model_ids",
        "playbook_hash",
        "error",
        # Added to `RunRecord` by the eval work in #38, and classified here
        # because `observable` refuses a field nobody has classified.
        #
        # `tool_budget` is the ceiling the harness configured, not a verdict on
        # the run. The graph already carries it in state, so the agent knew it
        # while it worked. `within_tool_budget` was observable already; the
        # number it was measured against belongs on the same side.
        "tool_budget",
        # `execution_trace` is the run's account of its own orchestration --
        # model calls, the graph path, replans, rollbacks, and per-agent token
        # counts, sanitized of prompts by contract. All of it is behaviour the
        # agent produced. None of it comes from `eval/faults/manifest.yaml`,
        # which is the test the oracle boundary actually applies.
        "execution_trace",
    }
)

#: Ground truth and scoring. Reading any of these into a lesson contaminates the
#: playbook with the evaluator's answers. See this module's docstring.
ORACLE_FIELDS = frozenset(
    {
        "case_id",
        "fault_id",
        "complexity",
        "correct_root_cause",
        "root_cause_rank",
        "correct_action_proposed",
        "cleanup_verified",
        "verified_recovery",
        "harmful_actions",
        "passed",
        "failure_reasons",
    }
)

#: The longest statement a drafter may contribute. A lesson is one
#: generalisation; a paragraph is a postmortem, and it has to fit in a brief
#: beside everything else the agent is given.
MAX_STATEMENT = 320

#: The longest symptom phrase to put in a precondition. `precondition_matches`
#: tests containment in both directions, so a short phrase from the alert
#: matches the alert it came from and a whole sentence of it still matches.
#: Bounded so a 2KB alert cannot become a 2KB precondition.
MAX_SYMPTOM = 80

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")


class ReflexionRefused(Exception):
    """Reflexion was asked to read, or write, something it may not.

    Raised rather than returning a lesson with the offending field dropped: a
    proposal silently missing its provenance is worse than no proposal, because
    a reviewer cannot see what is absent.
    """


def observable(run: dict[str, Any]) -> dict[str, Any]:
    """The subset of a run record reflexion may read.

    A projection rather than a check, so there is one place where the boundary
    is enforced and no caller has to remember it. Unknown keys are refused
    rather than passed through: a field nobody has classified is exactly the
    one that turns out to be the oracle's.
    """
    unknown = set(run) - OBSERVABLE_FIELDS - ORACLE_FIELDS
    if unknown:
        raise ReflexionRefused(
            f"unclassified run-record field(s) {sorted(unknown)}: add them to "
            "OBSERVABLE_FIELDS or ORACLE_FIELDS in oncall_memory.reflexion. A new "
            "field must be classified by whoever adds it, not default into the "
            "agent's memory."
        )
    return {key: value for key, value in run.items() if key in OBSERVABLE_FIELDS}


def _clean(text: str, limit: int) -> str:
    """Collapse whitespace, strip control characters, bound the length.

    The control strip is the same guard the telemetry client uses on span names
    and for the same reason: this string is model output, it is rendered in a
    terminal review and in a brief, and a carriage return in it can forge a row.
    """
    flattened = _WHITESPACE.sub(" ", _CONTROL.sub(" ", text or "")).strip()
    if len(flattened) <= limit:
        return flattened
    return flattened[: limit - 1].rstrip() + "…"


def recovery_reading(run: dict[str, Any]) -> str:
    """What the run observed about recovery, in three states, not two.

    The middle state is the one worth a lesson, and it is the one a boolean
    verdict loses. A run that reported recovery which telemetry never confirmed
    is the failure captured in `docs/objective-1-live-run.md`: the gate closed
    over the agent's own write and the shop was still broken.
    """
    seen = observable(run)
    claimed = bool(seen.get("system_reported_recovery"))
    confirmed = bool(seen.get("independently_verified_recovery"))
    if claimed and confirmed:
        return "held"
    if claimed:
        return "unconfirmed"
    return "absent"


_FALLBACK = {
    "held": (
        "When {symptom}, the action applied to {service} cleared the signal and "
        "telemetry confirmed it. Prefer this path before escalating."
    ),
    "unconfirmed": (
        "When {symptom}, this run reported recovery on {service} that telemetry "
        "did not confirm. Treat a self-reported fix as unverified until the "
        "signal itself comes back."
    ),
    "absent": (
        "When {symptom}, no recovery was observed on {service} in this run. "
        "Whatever was tried here is not yet known to work."
    ),
}


def _statement(reading: str, *, service: str, symptom: str, drafter: Callable | None) -> str:
    """One sentence. The drafter's if it gives a usable one, else the template.

    A drafter that raises, returns something that is not a string, or returns
    nothing usable falls back rather than failing the proposal. Reflexion runs
    after the incident is over; a dead model provider must not be able to lose
    the run's only chance to leave a note.
    """
    deterministic = _FALLBACK[reading].format(service=service or "the service", symptom=symptom)
    if drafter is None:
        return _clean(deterministic, MAX_STATEMENT)
    try:
        drafted = drafter(
            {
                "recovery": reading,
                "service": service,
                "symptom": symptom,
                "deterministic": deterministic,
            }
        )
    except Exception:  # noqa: BLE001 - a drafter is a model call; it must not lose the proposal
        return _clean(deterministic, MAX_STATEMENT)
    if not isinstance(drafted, str):
        return _clean(deterministic, MAX_STATEMENT)
    cleaned = _clean(drafted, MAX_STATEMENT)
    return cleaned or _clean(deterministic, MAX_STATEMENT)


def propose(
    run: dict[str, Any],
    *,
    alert: str,
    service: str,
    drafter: Callable | None = None,
) -> Lesson:
    """A candidate lesson from one finished run, always `inferred`.

    `alert` and `service` are arguments and not read from the run on purpose.
    They are the incident's INPUTS -- what paged, and which service it named --
    and they are what a human had in front of them too. The run record's own
    notion of which service was at fault is the oracle's.

    Raises `ReflexionRefused` for a run with no identity: a lesson whose
    `derived_from` is empty cannot be deleted by `forget(incident_id)`, and the
    policy's rule that a lesson must not outlive the incident that justified it
    would be unenforceable for it.
    """
    seen = observable(run)
    derived_from = str(seen.get("source_incident_id") or seen.get("run_id") or "").strip()
    if not derived_from:
        raise ReflexionRefused(
            "a run record with neither source_incident_id nor run_id cannot justify a "
            "lesson: forget(incident_id) deletes lessons by derived_from, so a lesson "
            "with no provenance could never be withdrawn."
        )

    symptom = _clean(alert, MAX_SYMPTOM)
    if not symptom:
        raise ReflexionRefused(
            "a lesson needs the alert text: `symptom` is the precondition that keeps "
            "it from being applied to every future incident."
        )

    reading = recovery_reading(run)
    statement = _statement(reading, service=service, symptom=symptom, drafter=drafter)

    preconditions = {"service": _clean(service, MAX_SYMPTOM), "symptom": symptom}
    # A raise and not an `assert`: asserts vanish under `-O`, and this is the
    # boundary the whole module exists to hold. It is a tautology today, which
    # is the point -- it stops being one the moment someone adds a key here.
    extra = set(preconditions) - set(EVALUABLE_PRECONDITIONS)
    if extra:
        raise ReflexionRefused(
            f"precondition key(s) {sorted(extra)} are not evaluated by "
            "precondition_matches, so they would read as a guard and match everything. "
            "Widen EVALUABLE_PRECONDITIONS and precondition_matches first."
        )

    evidence = [
        f"run {derived_from}",
        f"recovery {reading}",
        f"tool_calls {seen.get('tool_calls', 0)}",
    ]
    if seen.get("rolled_back"):
        evidence.append("rolled back")

    digest = hashlib.sha256(f"{derived_from}|{statement}".encode()).hexdigest()[:8]
    return Lesson(
        lesson_id=f"les-{digest}",
        statement=statement,
        status=PROPOSED_STATUS,
        service=preconditions["service"],
        preconditions=preconditions,
        derived_from=derived_from,
        evidence_refs=tuple(evidence),
        reason="",
    )
