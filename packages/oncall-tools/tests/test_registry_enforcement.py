"""The two defences F1 found inert, wired to the registry and proved to bite.

`docs/threat-model.md` F1: `permissions.for_role` and `hooks.pre_tool` were
implemented, tested and called by nothing during a run. These tests exercise
them through `load_tools`, which is the function the graph actually calls, so a
regression here is a regression in the run path and not in a helper.

Both defences are OFF by default. That is deliberate and is not laziness: the
graph must opt in, because narrowing by default is actively harmful -- F2 of the
same document shows `apply_through` falling back to an in-process `set_flag`
when the registry lacks the tool, so a reads-only default would route the write
around MCP rather than block it.
"""

from __future__ import annotations

import pytest
from oncall_contracts.tools import Approval, fingerprint
from oncall_tools.hooks.approval_context import approved, current_approval
from oncall_tools.hooks.pre_tool import ApprovalDenied, ApprovalMismatch, ApprovalRequired
from oncall_tools.registry import ToolRegistry, load_tools

PAYLOAD = {"flag": "paymentFailure", "variant": "off"}


def _approval(tool: str = "set_flag", args: dict | None = None, **kw) -> Approval:
    return Approval(fingerprint=fingerprint(tool, args if args is not None else PAYLOAD), **kw)


# --- the approval context ------------------------------------------------


def test_outside_a_context_there_is_no_approval() -> None:
    """The default is None, so an enforcing registry refuses without being told to."""
    assert current_approval() is None


def test_an_approval_does_not_outlive_its_block_even_on_an_exception() -> None:
    """A leaked approval would authorise the NEXT write, which is the whole fear."""
    with pytest.raises(RuntimeError), approved(_approval(approved_by="disen")):
        assert current_approval() is not None
        raise RuntimeError("tool blew up")
    assert current_approval() is None


# --- enforcement through load_tools --------------------------------------


@pytest.fixture
def shop_host(monkeypatch) -> None:
    """The telemetry server refuses to start with no endpoints configured, and
    `_stdio_env` forwards exactly these names. Listing its tools needs the
    address to exist, not to answer."""
    monkeypatch.setenv("ASTRONOMY_SHOP_HOST", "localhost")


@pytest.fixture
def enforcing(monkeypatch) -> ToolRegistry:
    """A registry built the way the graph would build it, with stub servers."""
    return load_tools(servers=["ops_actions"], require_approval=True)


def test_a_write_with_no_approval_in_scope_is_refused(enforcing) -> None:
    assert "_approval_guard" in enforcing["set_flag"].invoke.__qualname__
    with pytest.raises(ApprovalRequired):
        enforcing["set_flag"].invoke(PAYLOAD)


def test_a_write_with_a_matching_approval_runs(enforcing) -> None:
    with approved(_approval(approved_by="disen")):
        result = enforcing["set_flag"].invoke(PAYLOAD)
    assert result


def test_an_approval_for_different_arguments_is_refused(enforcing) -> None:
    """Approval binds to the payload, not the tool name. This is the edit case:
    a human approved `paymentFailure off` and the call became `adFailure on`."""
    stale = _approval(args={"flag": "adFailure", "variant": "on"}, approved_by="disen")
    with approved(stale), pytest.raises(ApprovalMismatch):
        enforcing["set_flag"].invoke(PAYLOAD)


def test_a_refused_approval_is_not_an_absent_one(enforcing) -> None:
    with approved(_approval(approved_by="disen", granted=False)), pytest.raises(ApprovalDenied):
        enforcing["set_flag"].invoke(PAYLOAD)


def test_a_read_needs_no_approval_at_all(shop_host) -> None:
    """Enforcement must not make the agent ask permission to look at telemetry.

    `pre_tool` returns early for a read, so the guard is never attached to one.
    """
    registry = load_tools(servers=["telemetry"], require_approval=True)
    assert registry.names, "the telemetry server advertised no tools"
    for tool in registry:
        assert tool.read
        # Structural, and deliberately not a call: invoking these would hit a
        # live Prometheus. The guard is a distinct closure, so its presence is
        # readable from the qualname without running anything.
        assert "_approval_guard" not in tool.invoke.__qualname__, (
            f"read tool {tool.name} was wrapped in the approval guard"
        )


def test_without_require_approval_nothing_changes(enforcing) -> None:
    """The default path is byte-for-byte the behaviour the graph has today."""
    plain = load_tools(servers=["ops_actions"])
    assert plain["set_flag"].invoke(PAYLOAD)


# --- role narrowing ------------------------------------------------------


def test_a_diagnosis_registry_does_not_hold_the_write_tools(shop_host) -> None:
    """Both servers on purpose: the reads must survive the narrowing, or the
    filter would look like it worked by emptying the registry."""
    registry = load_tools(servers=["ops_actions", "telemetry"], role="diagnosis")
    assert "set_flag" not in registry
    assert "rollback_flag" not in registry
    assert registry.names, "diagnosis kept no tools at all, so nothing was proved"


def test_a_remediation_registry_keeps_them() -> None:
    registry = load_tools(servers=["ops_actions"], role="remediation")
    assert "set_flag" in registry


def test_an_unknown_role_raises_rather_than_narrowing_to_nothing() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        load_tools(servers=["ops_actions"], role="auditor")


def test_a_narrowed_registry_still_owns_its_sessions() -> None:
    """The bug this guards: `for_role` returns a lens whose `close()` is a no-op
    (`select()` passes `close=None`). Returning that lens from `load_tools`
    would make the caller's `close()` silently leak one session per server, and
    `build.py` closes exactly what `load_tools` returned."""
    registry = load_tools(servers=["ops_actions"], role="diagnosis")
    assert registry._close is not None
