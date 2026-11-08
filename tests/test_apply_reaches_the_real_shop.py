"""The acceptance criterion for routing `Shop.apply` through `mcp-ops-actions`.

THIS FILE IS A SPECIFICATION, AND IT IS SKIPPED UNTIL THE SEAM EXISTS. Nothing
here is a request to change how the graph is built -- `main.py`, `c1_single.py`
and `stubs.py` are P2's, and the point of the exercise is that P2 can defend
them. What this file does is state, executably, what "the write is real" has to
mean, so the change has a definition of done that something other than a demo
run can check.

WHAT IS BROKEN TODAY, MEASURED RATHER THAN ARGUED. `main.py:272` builds
`Solo(shop=LiveShop())`, and `LiveShop` overrides three of `Shop`'s ten public
methods -- `promql`, `log_search`, `traces`. `apply` is inherited from the
scripted fixture, so the live investigation path writes to a dict. Against a
temporary copy of the flagd document, with `paymentFailure` armed:

    apply("set_flag", {"flag": "paymentFailure", "variant": "off"})
      -> {"ok": True, "previous": "100%", "changed": True}
    faulted() -> False      metrics() -> {"error_rate": 0.01}
    the document on disk -> "100%"

Every line of that is a success, and the shop is still broken. `metrics()` is
`0.08 if self.faulted() else 0.01` (`stubs.py:141`), `faulted()` reads
`self.flags` (`stubs.py:133`), and `self.flags` is what `apply` just wrote
(`stubs.py:328`). So `verify_metrics` (`nodes/remediate.py:183`) grades the
agent's own write against `RECOVERY_LINE = 0.05` and reports `RECOVERED`. The
gate closes over the write it is supposed to be checking.

THE OTHER HALF IS ALREADY BUILT. `mcp-ops-actions` writes the document flagd
reads and refuses any `(flag, variant)` pair no runbook names, with the offer in
the message. Measured against the same temporary copy:

    set_flag("checkout_error_rate", "high")
      -> ActionNotAllowed: no runbook names 'high' as a fix for flag
         'checkout_error_rate'
    set_flag("paymentFailure", "100%")
      -> ActionNotAllowed: ...; this tool accepts ['off'] for 'paymentFailure'
    set_flag("paymentFailure", "off")
      -> {"previous": "100%", "changed": True}   and the document says "off"

`checkout_error_rate` is not a hypothetical: it is the flag the live model
proposed in both runs captured in `docs/objective-1-live-run.md`, with
`search_runbooks` having handed it `set_flag paymentFailure variant off`
verbatim. Routing `apply` at this seam refuses that proposal for free.

A NOTE ON A TEST THAT SAYS THE OPPOSITE.
`packages/oncall-graph/tests/test_astronomy_shop.py::test_live_shop_reads_telemetry_and_keeps_stub_writes`
asserts `shop.flags["paymentFailure"] == "off"` after `apply`, and its name
makes the intention explicit. That test is correct about today and has to be
retired or rewritten by its owner as part of this change; it is not an
oversight, and this file does not get to overrule it. Flagged here so the
conflict is found now rather than in CI.

WHY `armed()` MATTERS AS MUCH AS `apply()`. Writing through to the document and
leaving `armed()` reading the in-memory mirror would fix the invented-flag
problem and leave the fiction standing: a write that is refused at the file
layer, or lands in the wrong file -- `DEFAULT_FLAG_PATH` is relative, and the
stdio servers' `cwd` is still unpinned -- would still read back as recovery off
the mirror. `test_the_verification_gate_reads_the_document_and_not_the_write`
is the one that pins this, and it is the one the demo's fifth beat depends on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from oncall_graph.astronomy_shop import LiveShop
from oncall_tools.servers.ops_actions import ActionNotAllowed

#: The committed baseline, which this file reads and must never write. Arming a
#: fault in it is what `test_no_fault_is_enabled_in_the_committed_flag_baseline`
#: refuses, and it has caught exactly this mistake twice.
BASELINE = Path(__file__).resolve().parents[1] / "deploy" / "flagd" / "demo.flagd.json"

#: The variable `mcp-ops-actions` and the lab both read to find the live
#: document. Pointing it at a temporary copy is what keeps these tests off the
#: baseline.
FLAG_PATH_ENV = "FLAGD_CONFIG"

FIX = {"flag": "paymentFailure", "variant": "off"}
#: The flag the live model invented, twice, with the real one in its context.
INVENTED = {"flag": "checkout_error_rate", "variant": "high"}


def _seam_is_wired() -> bool:
    """Whether `apply` refuses a pair no runbook names.

    Behaviour, not introspection: a probe that looked for a method name would
    pass for a method that writes to the wrong place. The allowlist is checked
    before the document is read, so this needs no file and no network and
    cannot alter anything -- the scripted path appends to `self.writes` and
    returns, the wired path raises.

    Any raise counts as wired, hence the breadth of the catch: a probe that
    only recognised `ActionNotAllowed` would skip this whole file, silently, for
    a seam wired some other way. If the raise is for an unexpected reason these
    tests run and fail with it, which is more useful than a skip that hides it.
    """
    try:
        LiveShop(host="unused.invalid").apply("set_flag", dict(INVENTED))
    except Exception:  # noqa: BLE001 - any refusal means this is no longer the fixture
        return True
    return False


pytestmark = pytest.mark.skipif(
    not _seam_is_wired(),
    reason=(
        "Shop.apply is still the in-memory fixture: a live run reports RECOVERED "
        "over an unchanged flagd document. Wire the write at the LiveShop seam "
        "(oncall_graph/astronomy_shop.py) through oncall_tools.servers.ops_actions, "
        "and make armed()/metrics() read the document rather than the mirror, and "
        "this file is the definition of done."
    ),
)


def variant_on_disk(document: Path, flag: str) -> str:
    """The flag's variant, read as raw JSON.

    Deliberately not `oncall_contracts.flagd.current_variant`: the production
    helper is what the write path uses, and a test that asserted through it
    would agree with the code under test about where the variant lives.
    """
    return json.loads(document.read_text())["flags"][flag]["defaultVariant"]


@pytest.fixture
def armed_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the real baseline with `paymentFailure` armed, and nothing else.

    A copy because arming the committed document is what the guard refuses, and
    the real one because a hand-written flagd file would not have the variants
    the allowlist is built from.
    """
    document = tmp_path / "demo.flagd.json"
    content = json.loads(BASELINE.read_text())
    content["flags"]["paymentFailure"]["defaultVariant"] = "100%"
    document.write_text(json.dumps(content, indent=2))
    monkeypatch.setenv(FLAG_PATH_ENV, str(document))
    return document


@pytest.fixture
def shop(armed_document: Path) -> LiveShop:
    """The shop the live path builds. `host` is set so no telemetry URL is read:
    these tests are about the write, and `LiveShop()` with none of the three
    URLs set refuses to construct at all."""
    return LiveShop(host="unused.invalid")


def test_the_approved_fix_changes_the_document_flagd_reads(shop, armed_document) -> None:
    """Beat five of demo objective 1. Without this the agent announces a fix
    that nothing outside its own memory can observe."""
    assert variant_on_disk(armed_document, "paymentFailure") == "100%"

    result = shop.apply("set_flag", dict(FIX))

    assert variant_on_disk(armed_document, "paymentFailure") == "off"
    assert result["previous"] == "100%"
    assert result["changed"] is True


def test_the_verification_gate_reads_the_document_and_not_the_write(shop, armed_document) -> None:
    """THE ONE THAT KILLS THE FICTION.

    The fault is put back behind the shop's back, which is what a write that
    did not take looks like from here -- refused by the file layer, landed in
    another file, or reverted by whoever armed it. A shop whose `armed()` reads
    the document notices. A shop reading its own mirror reports recovery, which
    is what a live run did on 2026-11-07.
    """
    shop.apply("set_flag", dict(FIX))
    assert shop.faulted() is False
    assert shop.metrics()["error_rate"] < 0.05

    content = json.loads(armed_document.read_text())
    content["flags"]["paymentFailure"]["defaultVariant"] = "100%"
    armed_document.write_text(json.dumps(content, indent=2))

    assert shop.faulted() is True, "armed() is reading the write, not the shop"
    assert shop.metrics()["error_rate"] >= 0.05


def test_an_invented_flag_is_refused_and_says_what_it_would_accept(shop, armed_document) -> None:
    """Both captured live runs proposed `checkout_error_rate`. A refusal has to
    carry the alternative, or the next proposal is another guess."""
    with pytest.raises(ActionNotAllowed) as refusal:
        shop.apply("set_flag", dict(INVENTED))

    assert "checkout_error_rate" in str(refusal.value)
    assert variant_on_disk(armed_document, "paymentFailure") == "100%"
    assert "checkout_error_rate" not in json.loads(armed_document.read_text())["flags"]


def test_a_rearm_cannot_be_routed_through_the_fix_tool(shop, armed_document) -> None:
    """Every fix in the corpus is `off` and every rollback restores a fault
    variant, and the two sets are disjoint. `set_flag` accepting `100%` would be
    a tool that can arm any fault in the corpus, told apart from the fix by a
    variant string."""
    with pytest.raises(ActionNotAllowed) as refusal:
        shop.apply("set_flag", {"flag": "paymentFailure", "variant": "100%"})

    assert "off" in str(refusal.value), "a refusal with no offer invites a guess"


def test_restart_cannot_be_applied_because_no_server_implements_it(shop) -> None:
    """`ACTION_TOOLS` at `planning/plan.py:27` still accepts `restart`, so the
    planner can propose it and `validate_action` will pass it. There is no
    `restart` tool on any server, and this is where that becomes true of the
    write path rather than a matter of the planner's tuple.

    Demo objective 2 wants a log line saying "restart everything" to be
    refused. A `restart` that silently appends to a list is the failure that
    objective is about.

    The requirement is that it does not report success, so the refusal is
    checked by outcome and the type is left open -- which tool layer says no is
    P2's call, and today the fixture appends the service to `self.restarts` and
    returns `{"ok": True}`.
    """
    try:
        result = shop.apply("restart", {"service": "payment"})
    except (ActionNotAllowed, ValueError, LookupError) as refusal:
        assert "restart" in str(refusal)
        return
    pytest.fail(f"restart reported {result!r}; no server implements it")


def test_a_flag_already_off_is_not_a_fix_that_worked(shop, armed_document) -> None:
    """`changed: false` is the difference between "I fixed it" and "there was
    nothing here to fix". The demo narration must not read the second as the
    first, and neither must a reflexion note."""
    shop.apply("set_flag", dict(FIX))
    again = shop.apply("set_flag", dict(FIX))

    assert again["changed"] is False
    assert again["previous"] == "off"


def test_these_tests_never_write_the_committed_baseline() -> None:
    """The guard that refuses an armed baseline has caught me twice. This keeps
    the acceptance criterion itself from being the thing that trips it."""
    assert variant_on_disk(BASELINE, "paymentFailure") == "off"
