"""The playbook: approved lessons, and a hash that pins which playbook ran.

The interface is the one `oncall_graph.handoffs.load_lessons` already expects --
an object with `search(service, symptom, status)` -- so the graph never imports
this package. `oncall-service` builds the store and injects it.

STILL READ ONLY, AND NOW FOR A SHARPER REASON. Writing lives in
`oncall_memory.proposals`: reflexion appends a candidate, a person appends a
decision, and this class reads the result. There is no `add` or `approve` HERE
because a store that could grow itself is a store an incident could grow
mid-run, and `docs/memory-policy.md` is explicit that a lesson enters the
playbook only through a person.

What this class now does is MERGE the two halves: the committed seed corpus in
`data/postmortems/`, and the lessons a person has approved in the proposal log.
Both arrive as `approved` lessons and are indistinguishable to the graph, which
is right -- provenance is in `derived_from`, not in which file it came from.

`forget(incident_id)` is the next change and belongs on the log, not here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .postmortems import (
    APPLICABLE_STATUS,
    Lesson,
    Postmortem,
    load_postmortems,
)
from .proposals import ProposalLog


class LessonStore:
    """Every lesson the postmortems justify, queried by provenance status."""

    def __init__(
        self,
        postmortems: tuple[Postmortem, ...] = (),
        proposals: ProposalLog | None = None,
    ) -> None:
        self._postmortems = tuple(postmortems)
        seeded = tuple(lesson for postmortem in self._postmortems for lesson in postmortem.lessons)
        self._proposals = proposals
        approved = tuple(proposals.lessons()) if proposals is not None else ()
        # A proposal that reuses a seed lesson's id RAISES. Merging silently
        # would let an approved proposal shadow a committed lesson -- including
        # a committed REJECTED one, which is how a refused lesson would come
        # back as guidance through a side door.
        clash = {lesson.lesson_id for lesson in seeded} & {lesson.lesson_id for lesson in approved}
        if clash:
            raise ValueError(
                f"lesson id(s) {sorted(clash)} are in both the committed corpus and the "
                "proposal log: an approved proposal must not shadow a seed lesson, least "
                "of all a rejected one."
            )
        self._lessons: tuple[Lesson, ...] = seeded + approved

    @classmethod
    def from_postmortems(
        cls, directory: Path | None = None, proposals: ProposalLog | None = None
    ) -> LessonStore:
        """Load `data/postmortems/*.md`. Raises `PostmortemError` on an invalid corpus.

        Pass a `ProposalLog` to include the lessons a person has approved. Left
        out, the store is exactly the committed corpus -- which is what a
        held-out evaluation run wants, since the policy freezes the playbook and
        writes no new lessons during one.
        """
        return cls(load_postmortems(directory), proposals)

    def __len__(self) -> int:
        return len(self._lessons)

    @property
    def postmortems(self) -> tuple[Postmortem, ...]:
        return self._postmortems

    @property
    def playbook_hash(self) -> str:
        """A stable hash of the APPLICABLE playbook, for `RunRecord.playbook_hash`.

        Over approved lessons only, sorted by id, so it answers the question the
        evaluation boundary asks: did these two runs apply the same guidance?
        Editing a rejected lesson's reason does not change it; approving one
        does. `docs/memory-policy.md` requires the playbook frozen before any
        held-out run, and a hash is what makes "frozen" checkable rather than
        promised.
        """
        applicable = sorted(
            (lesson.as_dict() for lesson in self._lessons if lesson.status == APPLICABLE_STATUS),
            key=lambda row: row["id"],
        )
        canonical = json.dumps(applicable, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def search(
        self,
        *,
        service: str = "",
        symptom: str = "",
        status: str | None = APPLICABLE_STATUS,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Lessons with this provenance status. Filters on STATUS ONLY.

        `service` and `symptom` are accepted because that is the interface
        `load_lessons` calls, and they are deliberately NOT used to narrow the
        result. Two reasons, and the second is the one that matters:

        * Precondition evaluation belongs to the graph, which owns the live case
          and returns BOTH the kept and the dropped lessons.
        * Demo scene 3 shows an old lesson being rejected because its
          preconditions do not match, and what it renders is that dropped list. A
          store that narrowed first would hand over nothing to reject, and the
          scene would quietly become an empty list -- a demo objective lost to an
          optimisation, with no error anywhere.

        `status=None` means approved, not "everything". An unreviewed lesson
        reaching the model as guidance is the failure this store exists to
        prevent, so the permissive reading of a missing filter is not available.

        Asking for any other status RAISES rather than returning those lessons.
        `search` is the path a brief is built from, so there must be no argument
        that routes a refused or unreviewed lesson into one -- not even an
        explicit one. `rejected()` is the human path, and it is a different
        method for exactly this reason.
        """
        wanted = status or APPLICABLE_STATUS
        if wanted != APPLICABLE_STATUS:
            raise ValueError(
                f"search serves {APPLICABLE_STATUS!r} lessons only, not {wanted!r}: "
                "an unreviewed, refused or superseded lesson is not guidance. "
                "Use rejected() to show a person a refusal and its reason."
            )
        rows = [lesson.as_dict() for lesson in self._lessons if lesson.status == wanted]
        rows.sort(key=lambda row: row["id"])
        return rows[: max(0, limit)]

    def rejected(self) -> list[dict[str, Any]]:
        """Refused lessons, with their reasons. For a person, never as guidance.

        The policy keeps these on purpose: one rejected lesson and its reason is
        a required deliverable, and a refusal is a useful counterexample. It is a
        separate method from `search` so no status argument can route a rejected
        lesson into a brief by accident.
        """
        return [
            lesson.as_dict()
            for lesson in sorted(self._lessons, key=lambda item: item.lesson_id)
            if lesson.status == "rejected"
        ]
