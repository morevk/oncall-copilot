"""P1 — the lesson store. See `docs/memory-policy.md` for what may be stored.

Read side: `LessonStore`, which serves `approved` lessons only.
Write side: `reflexion.propose` offers a candidate, `ProposalLog` records it, and
a person decides through `python -m oncall_memory.review`. Nothing in this
package can approve a lesson on its own.
"""

from .postmortems import (
    APPLICABLE_STATUS,
    DEFAULT_POSTMORTEM_DIR,
    EVALUABLE_PRECONDITIONS,
    LESSON_STATUSES,
    Lesson,
    Postmortem,
    PostmortemError,
    load_postmortems,
)
from .proposals import (
    DEFAULT_LOG_PATH,
    ProposalLog,
    ProposalLogError,
)
from .reflexion import (
    OBSERVABLE_FIELDS,
    ORACLE_FIELDS,
    PROPOSED_STATUS,
    ReflexionRefused,
    observable,
    propose,
    recovery_reading,
)
from .store import LessonStore

__all__ = [
    "APPLICABLE_STATUS",
    "DEFAULT_LOG_PATH",
    "DEFAULT_POSTMORTEM_DIR",
    "EVALUABLE_PRECONDITIONS",
    "LESSON_STATUSES",
    "OBSERVABLE_FIELDS",
    "ORACLE_FIELDS",
    "PROPOSED_STATUS",
    "Lesson",
    "LessonStore",
    "Postmortem",
    "PostmortemError",
    "ProposalLog",
    "ProposalLogError",
    "ReflexionRefused",
    "load_postmortems",
    "observable",
    "propose",
    "recovery_reading",
]
