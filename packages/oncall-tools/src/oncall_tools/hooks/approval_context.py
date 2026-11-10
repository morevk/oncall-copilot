"""The approval in scope for the current tool call.

`pre_tool` needs an `Approval` whose fingerprint matches the payload about to
run. It cannot be a tool ARGUMENT: the registry hands the model a callable whose
keyword arguments are the tool's own schema, so an `approval=` keyword would
collide with any tool that ever has a field of that name, and the LangChain
adapter on the other side of the seam passes tool arguments as `**kwargs`.

So the approval travels out of band, in a ContextVar the caller opens around the
call it has permission for:

    with approved(approval):
        registry["set_flag"].invoke({"flag": "paymentFailure", "variant": "off"})

A ContextVar and not a module global, because the bridge in `registry` runs the
MCP session on its own thread and a graph may run incidents concurrently; a
global would let one incident's approval authorise another's write.

DEFAULT IS NONE, AND THAT IS THE POINT. Outside a context there is no approval,
so an enforcing registry refuses every write. Nothing has to remember to deny.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from oncall_contracts.tools import Approval

_CURRENT: ContextVar[Approval | None] = ContextVar("oncall_tools_approval", default=None)


def current_approval() -> Approval | None:
    """The approval in scope, or None. `pre_tool` turns None into a refusal."""
    return _CURRENT.get()


@contextmanager
def approved(approval: Approval | None) -> Iterator[None]:
    """Put one approval in scope for the body.

    Accepts None so a caller can be explicit about having no approval -- which
    reads better at a call site than not opening the context at all, and makes
    the refusal path testable.

    The token is reset in `finally`, so an approval cannot outlive the block
    even if the tool raises. An approval that leaked past its block would
    authorise the next write, which is the failure this whole gate exists to
    prevent.
    """
    token = _CURRENT.set(approval)
    try:
        yield
    finally:
        _CURRENT.reset(token)
