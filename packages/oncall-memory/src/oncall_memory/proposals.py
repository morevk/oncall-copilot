"""The proposal log: an append-only record of lessons offered, approved, refused.

APPEND ONLY, BECAUSE `docs/memory-policy.md` SAYS SO AND BECAUSE THE ORIGINAL IS
EVIDENCE. "Correct a record: append a superseding record referencing the
original; never edit in place... because 'we believed X at 10:04' is itself
evidence when reconstructing a timeline." So approving a lesson does not rewrite
the proposal; it appends a decision about it, and the proposal stays with the
statement that was actually reviewed. A reviewer who later asks "what was I
shown?" has an answer, which they would not if approval overwrote the row.

The file lives under `artifacts/`, which `.gitignore` excludes, for the reason
the policy gives: the playbook is reproducible and deleting it loses nothing
else. The seed corpus in `data/postmortems/` is the committed part; this log is
what a running deployment accumulates on top of it.

A MALFORMED LINE RAISES. It would be easy to skip one and carry on, and the
result would be a playbook quietly missing whichever lessons were in the
unreadable rows -- an empty `pending()` reading as "nothing to review" rather
than as "this log could not be read". That is the project's recurring failure in
its exact shape, so this module refuses to guess.

NO `forget()` HERE YET. It is the next change, and it deletes by `derived_from`,
which is why every record carries it and why `reflexion.propose` refuses a run
with no identity.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .postmortems import APPLICABLE_STATUS, Lesson

#: Where the log lives unless told otherwise. Under `artifacts/`, gitignored.
LOG_PATH_ENV = "LESSON_LOG"
DEFAULT_LOG_PATH = Path(__file__).resolve().parents[4] / "artifacts" / "lessons.jsonl"

PROPOSED = "proposed"
APPROVED = "approved"
REJECTED = "rejected"
_DECISIONS = (APPROVED, REJECTED)

#: The status each event puts on the lesson it resolves. Public because the
#: review command needs it to show a lesson's CURRENT status: a decision record
#: carries the lesson as it was reviewed, which still reads `inferred`.
STATUS_FOR = {PROPOSED: "inferred", APPROVED: APPLICABLE_STATUS, REJECTED: "rejected"}


class ProposalLogError(Exception):
    """The log could not be read, or the write would have broken its invariants."""


def log_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    configured = os.environ.get(LOG_PATH_ENV)
    return Path(configured) if configured else DEFAULT_LOG_PATH


class ProposalLog:
    """Lessons proposed by reflexion, and the decisions people made about them."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = log_path(path)

    # ---- reading

    def records(self) -> list[dict[str, Any]]:
        """Every event, oldest first. A missing file is empty; a bad line raises."""
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProposalLogError(
                    f"{self.path}:{number} is not readable JSON ({exc}). Refusing to "
                    "skip it: a playbook missing the lessons in an unreadable row would "
                    "report as a playbook with nothing to review."
                ) from exc
            if not isinstance(row, dict) or row.get("event") not in STATUS_FOR:
                raise ProposalLogError(
                    f"{self.path}:{number} is not a proposal-log event: {line[:120]!r}"
                )
            rows.append(row)
        return rows

    def _latest(self) -> dict[str, dict[str, Any]]:
        """The last event per lesson id, which is its current state."""
        state: dict[str, dict[str, Any]] = {}
        for row in self.records():
            lesson_id = str((row.get("lesson") or {}).get("id") or "")
            if not lesson_id:
                raise ProposalLogError(f"{self.path}: an event carries no lesson id: {row!r}")
            state[lesson_id] = row
        return state

    def _with_status(self, event: str) -> list[dict[str, Any]]:
        rows = [
            {**row["lesson"], "status": STATUS_FOR[event], "reason": row.get("reason") or ""}
            for row in self._latest().values()
            if row["event"] == event
        ]
        rows.sort(key=lambda row: row["id"])
        return rows

    def pending(self) -> list[dict[str, Any]]:
        """Proposals nobody has decided on. `inferred`, and never guidance."""
        return self._with_status(PROPOSED)

    def approved(self) -> list[dict[str, Any]]:
        """Lessons a person accepted. These join the playbook."""
        return self._with_status(APPROVED)

    def rejected(self) -> list[dict[str, Any]]:
        """Refusals, each with the reason that was required to record it."""
        return self._with_status(REJECTED)

    def history(self, lesson_id: str) -> list[dict[str, Any]]:
        """Every event for one lesson, oldest first -- what was shown, then decided."""
        return [row for row in self.records() if (row.get("lesson") or {}).get("id") == lesson_id]

    def lessons(self) -> Iterator[Lesson]:
        """Approved proposals as `Lesson` objects, for `LessonStore` to merge."""
        for row in self.approved():
            yield Lesson(
                lesson_id=row["id"],
                statement=row["statement"],
                status=APPLICABLE_STATUS,
                service=row.get("service", ""),
                preconditions=dict(row.get("preconditions") or {}),
                derived_from=row.get("derived_from", ""),
                evidence_refs=tuple(row.get("evidence_refs") or ()),
                reason="",
            )

    # ---- writing

    def _append(self, row: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        return row

    def propose(self, lesson: Lesson, *, at: str | None = None) -> dict[str, Any]:
        """Record a candidate lesson. Refuses anything already proposed or decided.

        The status is taken from the lesson and must be `inferred`: this method
        is the only way into the log, so accepting an `approved` lesson here
        would be a path that puts guidance in the playbook with no person in it.
        """
        if lesson.status != STATUS_FOR[PROPOSED]:
            raise ProposalLogError(
                f"a proposal must be {STATUS_FOR[PROPOSED]!r}, not {lesson.status!r}: "
                "approving is a separate, recorded act by a person."
            )
        existing = self._latest().get(lesson.lesson_id)
        if existing is not None:
            raise ProposalLogError(
                f"{lesson.lesson_id} is already in the log as {existing['event']!r}. "
                "The id is derived from the run and the statement, so this is the same "
                "proposal twice; a re-proposal after a rejection needs a new statement."
            )
        return self._append(
            {
                "event": PROPOSED,
                "at": at or _now(),
                "lesson": lesson.as_dict(),
            }
        )

    def _decide(
        self, lesson_id: str, event: str, *, by: str, reason: str, at: str | None
    ) -> dict[str, Any]:
        if not by.strip():
            raise ProposalLogError(
                "a decision needs an approver: the policy requires the audit trail to "
                "name who accepted or refused a lesson."
            )
        current = self._latest().get(lesson_id)
        if current is None:
            raise ProposalLogError(f"no proposal {lesson_id!r} in {self.path}")
        if current["event"] in _DECISIONS:
            raise ProposalLogError(
                f"{lesson_id} was already {current['event']} at {current.get('at')}. "
                "Decisions are appended, never replaced; supersede it instead of "
                "deciding twice."
            )
        return self._append(
            {
                "event": event,
                "at": at or _now(),
                "by": by.strip(),
                "reason": reason.strip(),
                # The lesson as REVIEWED, carried forward verbatim. The decision
                # has to name what was decided on, or a later edit upstream would
                # silently change what someone approved.
                "lesson": dict(current["lesson"]),
            }
        )

    def approve(self, lesson_id: str, *, by: str, at: str | None = None) -> dict[str, Any]:
        """Accept a proposal into the playbook. This changes `playbook_hash`."""
        return self._decide(lesson_id, APPROVED, by=by, reason="", at=at)

    def reject(
        self, lesson_id: str, *, by: str, reason: str, at: str | None = None
    ) -> dict[str, Any]:
        """Refuse a proposal, with the reason the policy requires.

        A refusal with no reason is indistinguishable from an oversight, and the
        guide wants one rejected lesson and its reason as a deliverable. So the
        reason is not optional, and an empty one raises rather than recording a
        refusal nobody can learn from.
        """
        if not reason.strip():
            raise ProposalLogError(
                f"rejecting {lesson_id} needs a reason: a refusal with none is "
                "indistinguishable from an oversight, and a rejected lesson is kept "
                "precisely as a counterexample."
            )
        return self._decide(lesson_id, REJECTED, by=by, reason=reason, at=at)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
