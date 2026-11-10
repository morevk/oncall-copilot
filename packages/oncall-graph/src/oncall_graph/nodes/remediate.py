"""The approval, verify, and rollback path shared by the solo agent and the team.

The model proposes one allow-listed action. This code is what runs it. Approve
executes that payload only when its hash matches the interrupt. An edited
payload interrupts again. Reject returns without a shop write. A failed
verification rolls the action back in code.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time

from langgraph.types import interrupt
from oncall_tools.registry import ToolCallError

from ..evidence import CLIENT_CHARGE, SERVER_CHARGE
from ..planning.monitor import code_rollback
from ..planning.plan import default_rollback, validate_action
from ..state import transcript_lines

# The checkout alert fires above 5%. The scripted shop still uses this line.
# A live paymentFailure run does not: recovery is successes returning.
RECOVERY_LINE = 0.05

# eval/faults/manifest.yaml, payment-failure recovery. One success inside the
# window makes the rate positive, so a recovery returns before five minutes.
RECOVERY_QUERY = "sum(rate(demo_payment_transactions_total[5m]))"
# The detect query's 422 numerator. Read only once the window has elapsed, so
# failures from before the fix have aged out of the same five-minute rate.
CHECKOUT_FAILURE_QUERY = (
    "sum(rate(http_server_request_duration_seconds_count{"
    'service_name="frontend",http_response_status_code="422"}[5m]))'
)
RECOVERY_FLAG = "paymentFailure"
# paymentUnreachable: checkout's Charge client calls are erroring and the
# payment server is receiving none. The two queries live in evidence.py.
UNREACHABLE_FLAG = "paymentUnreachable"
RECOVERY_WINDOW_SECONDS = 300
RECOVERY_PAUSE_SECONDS = 10
# Pauses sit between polls, so the last poll lands on the window.
RECOVERY_POLLS = RECOVERY_WINDOW_SECONDS // RECOVERY_PAUSE_SECONDS + 1


def command_payload(action) -> dict:
    """The exact command the engineer approves: action, args, and expected effect."""
    return {
        "action": action["action"],
        "args": dict(action.get("args") or {}),
        "expected_effect": action.get("expected_effect") or "",
    }


def payload_hash(payload) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def apply_command(action, command):
    """A new command on top of the previous proposal. Invalid commands raise."""
    if not isinstance(command, dict):
        # ValueError, not TypeError: the approval node catches it and interrupts again.
        raise ValueError("command must be an object")  # noqa: TRY004
    if "action" not in command and isinstance(command.get("command"), dict):
        command = command["command"]
    name = command.get("action") or action["action"]
    args = command.get("args") if "args" in command else action.get("args")
    effect = command.get("expected_effect") or action.get("expected_effect")
    proposed = {"action": name, "args": args, "expected_effect": effect}
    if command.get("rollback") is None:
        proposed["rollback"] = default_rollback(name, args if isinstance(args, dict) else {})
    else:
        proposed["rollback"] = command["rollback"]
    return validate_action(proposed)


def _decision(resume):
    if isinstance(resume, str):
        return {"decision": resume.strip().lower()}
    if isinstance(resume, dict):
        decision = dict(resume)
        choice = decision.get("decision")
        if isinstance(choice, str):
            decision["decision"] = choice.strip().lower()
        return decision
    return {}


def approval(state):
    """Interrupt with the command, its expected effect, and the payload hash.

    Resume with ``Command(resume=...)``. Approve runs this payload only when
    the hash matches. A different command or a hash from an earlier payload
    interrupts again and does not write. Reject ends the run with no write.

    The node starts over on each resume, and earlier resume values are replayed
    in order, so an edit made on the previous pause is applied before the new
    decision is read.
    """
    current = state.get("proposed_action")
    if not current:
        return {
            "status": "REJECTED",
            "route": "reject",
            "why": "no action to approve",
            "approval_payload_hash": "",
            "transcript": transcript_lines("approval", "approval rejected"),
        }
    action = copy.deepcopy(current)
    error = None
    while True:
        payload = command_payload(action)
        digest = payload_hash(payload)
        shown = {
            "command": {"action": payload["action"], "args": payload["args"]},
            "expected_effect": payload["expected_effect"],
            "payload_hash": digest,
        }
        if error:
            shown["error"] = error
            error = None
        decision = _decision(interrupt(shown))
        supplied = decision.get("command")
        if (
            isinstance(supplied, dict)
            and decision.get("expected_effect")
            and "expected_effect" not in supplied
        ):
            supplied = {**supplied, "expected_effect": decision["expected_effect"]}
        if supplied:
            try:
                edited = apply_command(action, supplied)
            except ValueError as exc:
                error = str(exc)
                continue
            if payload_hash(command_payload(edited)) != digest:
                action = edited
                continue
        choice = decision.get("decision") or ""
        if choice in ("reject", "rejected"):
            return {
                "status": "REJECTED",
                "route": "reject",
                "why": "engineer rejected the action",
                "approval_payload_hash": "",
                "transcript": transcript_lines("approval", "approval rejected"),
            }
        if choice in ("approve", "approved"):
            claimed = decision.get("payload_hash")
            if claimed not in (None, "", digest):
                continue
            plan = dict(state.get("plan") or {})
            plan["stage"] = "act"
            return {
                "plan": plan,
                "stage": "act",
                "proposed_action": action,
                "approval_payload_hash": digest,
                "route": "execute",
                "status": "APPROVED",
                "why": "",
                "transcript": transcript_lines("approval", "approval matched"),
            }
        continue


def apply_through(shop, tool, args, tools=None) -> dict:
    """Run one approved write.

    A registry that has the tool is the shop: ``set_flag`` and ``rollback_flag``
    run in ``mcp-ops-actions``, which holds the flagd document. That path does
    not append to ``shop.writes``. The caller records the write on
    ``executed_writes``. The scripted ``Shop.apply`` remains for a graph built
    without that registry, and it keeps its own list. The returned variant is
    copied onto ``shop.flags`` so ``verify_metrics`` does not roll back a write
    that landed; ``LiveShop.armed`` does not trust that copy.
    """
    clean = dict(args or {})
    if tools is not None and tool in tools:
        invoke = getattr(tools[tool], "invoke", None)
        if not callable(invoke):
            raise TypeError(f"{tool} has no invoke")
        reported = invoke(clean)
        if not isinstance(reported, dict):
            raise TypeError(f"{tool} returned {type(reported).__name__}")
        result = {
            "ok": True,
            "tool": tool,
            "args": clean,
            "flag": reported.get("flag", clean.get("flag")),
            "variant": reported.get("variant", clean.get("variant")),
            "previous": reported.get("previous"),
            "changed": reported.get("changed"),
        }
        flag = result.get("flag")
        variant = result.get("variant")
        if (
            shop is not None
            and hasattr(shop, "flags")
            and isinstance(flag, str)
            and isinstance(variant, str)
        ):
            shop.flags[flag] = variant
        return result
    return shop.apply(tool, clean)


def execute_action_code(state, shop, tools=None):
    """Run the approved command. A different hash does not touch the shop."""
    action = state.get("proposed_action") or {}
    if not action:
        return {
            "status": "APPROVAL_MISMATCH",
            "route": "stop",
            "why": "no action to execute",
            "transcript": transcript_lines("execute", "execute refused"),
        }
    digest = payload_hash(command_payload(action))
    if digest != (state.get("approval_payload_hash") or ""):
        return {
            "status": "APPROVAL_MISMATCH",
            "route": "stop",
            "why": "approval hash does not match the command",
            "transcript": transcript_lines("execute", "execute refused"),
        }
    result = apply_through(shop, action["action"], action["args"], tools)
    plan = dict(state.get("plan") or {})
    plan["stage"] = "verify"
    record = {"tool": action["action"], "args": dict(action["args"])}
    return {
        "plan": plan,
        "stage": "verify",
        "last": {"step": record, "result": result},
        "executed_writes": [record],
        "ok": True,
        "route": "verify",
        "transcript": transcript_lines("execute", f"executed {action['action']}"),
    }


def _promql(tools, query: str) -> dict:
    invoke = getattr(tools["promql"], "invoke", None)
    if not callable(invoke):
        return {"error": "promql has no invoke"}
    try:
        reading = invoke({"query": query})
    except (ToolCallError, TypeError, ValueError) as exc:
        return {"error": str(exc)}
    if not isinstance(reading, dict):
        return {"error": f"promql returned {type(reading).__name__}"}
    return reading


def _sample(reading: dict) -> float | None:
    """The last value of the first series. An empty result is a rate of zero."""
    if reading.get("error"):
        return None
    series = reading.get("series")
    if not isinstance(series, list):
        return None
    if not series:
        return 0.0
    head = series[0]
    value = head.get("value") if isinstance(head, dict) else None
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return float(value[1])
        except (TypeError, ValueError):
            return None
    return None


def _verify_update(state, *, recovered: bool, pending: bool, signal: str, ref: str, detail: str):
    plan = dict(state.get("plan") or {})
    plan["stage"] = "communicate" if recovered else "verify"
    if recovered:
        route, status, why = "recovered", "RECOVERED", ""
    elif pending:
        route, status, why = "pending", "APPLIED", "applied, not yet verified"
    else:
        route, status, why = "rollback", state.get("status") or "RUNNING", "verification failed"
    return {
        "plan": plan,
        "stage": plan["stage"],
        "verification": {
            "recovered": recovered,
            "pending": pending,
            "signal": signal,
            "evidence_refs": [ref],
        },
        "route": route,
        "status": status,
        "why": why,
        "transcript": transcript_lines("verify", detail),
    }


def _checkout_failures_still_arriving(tools) -> bool | None:
    """True when post-fix checkout traffic is still failing. None when unread."""
    reading = _promql(tools, CHECKOUT_FAILURE_QUERY)
    sample = _sample(reading)
    if sample is None:
        return None
    return sample > 0


def _applied_update(
    state,
    last_error: str,
    *,
    signal: str = "demo_payment_transactions_total",
    ref: str = RECOVERY_QUERY,
) -> dict:
    detail = "verify applied, not yet verified"
    if last_error:
        detail = f"{detail}: {last_error}"
    return _verify_update(
        state,
        recovered=False,
        pending=True,
        signal=signal,
        ref=ref,
        detail=detail,
    )


def _verify_recovery_query(state, tools, *, attempts: int, pause) -> dict:
    """Poll the payment-success rate across the five-minute window.

    A success returns immediately. The last poll is the failure check: checkout
    422s still arriving means the fix failed, and a quiet shop stays applied.
    """
    last_error = ""
    for attempt in range(attempts):
        reading = _promql(tools, RECOVERY_QUERY)
        sample = _sample(reading)
        if sample is None:
            last_error = str(reading.get("error") or "promql returned no sample")
        elif sample > 0:
            return _verify_update(
                state,
                recovered=True,
                pending=False,
                signal="demo_payment_transactions_total",
                ref=RECOVERY_QUERY,
                detail="verify recovered=True",
            )
        else:
            last_error = ""
        if sample == 0 and attempt + 1 == attempts:
            still_failing = _checkout_failures_still_arriving(tools)
            if still_failing is True:
                return _verify_update(
                    state,
                    recovered=False,
                    pending=False,
                    signal="http_server_request_duration_seconds_count",
                    ref=CHECKOUT_FAILURE_QUERY,
                    detail="verify recovered=False",
                )
            if still_failing is None:
                last_error = "checkout failure query returned no sample"
        if attempt + 1 < attempts:
            pause()
    return _applied_update(state, last_error)


def _verify_unreachable(state, tools, *, attempts: int, pause) -> dict:
    """Poll the Charge pair across the same five-minute window.

    Checkout client errors stay in the rate after the flag is cleared, so a
    server Charge rate above zero is recovery and returns immediately. The
    last poll is the failure check the fault is named by: client errors still
    above zero while the payment server Charge rate is still zero. Both zero,
    or a query that cannot be read, stays applied.
    """
    last_error = ""
    for attempt in range(attempts):
        reading = _promql(tools, SERVER_CHARGE)
        server = _sample(reading)
        if server is None:
            last_error = str(reading.get("error") or "promql returned no sample")
        elif server > 0:
            return _verify_update(
                state,
                recovered=True,
                pending=False,
                signal="SPAN_KIND_SERVER",
                ref=SERVER_CHARGE,
                detail="verify recovered=True",
            )
        else:
            last_error = ""
        if server == 0 and attempt + 1 == attempts:
            client_reading = _promql(tools, CLIENT_CHARGE)
            client = _sample(client_reading)
            if client is None:
                last_error = "checkout charge query returned no sample"
            elif client > 0:
                return _verify_update(
                    state,
                    recovered=False,
                    pending=False,
                    signal="SPAN_KIND_CLIENT",
                    ref=CLIENT_CHARGE,
                    detail="verify recovered=False",
                )
        if attempt + 1 < attempts:
            pause()
    return _applied_update(state, last_error, signal="SPAN_KIND_SERVER", ref=SERVER_CHARGE)


def _scripted_recovery(state, shop) -> dict:
    """The fixture has no lag. A flag left armed is a failed fix and rolls back."""
    reading = shop.metrics()
    rate = float(reading.get("error_rate") or 0)
    recovered = rate < RECOVERY_LINE
    return _verify_update(
        state,
        recovered=recovered,
        pending=False,
        signal="checkout_error_rate",
        ref="checkout_error_rate",
        detail=f"verify recovered={recovered}",
    )


def verify_metrics(state, shop, tools=None, *, attempts: int | None = None, pause=None):
    """Decide recovery in code. The model does not.

    A live ``paymentFailure`` run polls the manifest recovery query through
    ``promql``. One success returns recovered immediately. The last poll waits
    out the five-minute window, so the rate no longer contains samples from
    before the fix. Still zero then, with checkout 422s still arriving, is a
    failed fix and takes the rollback edge. Still zero with a quiet checkout
    stays applied, and the flag is not rolled back.

    A live ``paymentUnreachable`` run polls the same window. A payment server
    Charge rate above zero returns recovered immediately. On the last poll,
    checkout Charge client errors still above zero while that server rate is
    still zero is a failed fix and takes the rollback edge. Both zero, or a
    query that cannot be read, stays applied.

    Any other case reads the scripted shop, whose rate moves the moment the
    fixture flag changes.
    """
    action = state.get("proposed_action") or {}
    flag = (action.get("args") or {}).get("flag")
    if tools is not None and "promql" in tools and flag in {RECOVERY_FLAG, UNREACHABLE_FLAG}:
        waits = RECOVERY_POLLS if attempts is None else attempts
        if waits < 1:
            raise ValueError("recovery polls must be at least 1")
        pause_fn = pause or (lambda: time.sleep(RECOVERY_PAUSE_SECONDS))
        if flag == UNREACHABLE_FLAG:
            return _verify_unreachable(state, tools, attempts=waits, pause=pause_fn)
        return _verify_recovery_query(state, tools, attempts=waits, pause=pause_fn)
    return _scripted_recovery(state, shop)


def rollback_code(state, shop, tools=None):
    """Apply the inverse action in code. A prose rollback is recorded, not shelled."""
    pending = code_rollback(state)
    update = {
        "pending_rollback": pending,
        "route": "replan",
        "transcript": transcript_lines("rollback", "rollback"),
    }
    if pending.get("tool"):
        args = dict(pending.get("args") or {})
        result = apply_through(shop, pending["tool"], args, tools)
        record = {"tool": pending["tool"], "args": args}
        update["executed_writes"] = [record]
        update["last"] = {"step": record, "result": result}
    else:
        update["last"] = {"step": pending, "result": {"ok": False, "skipped": True}}
    return update


def route_approval(state) -> str:
    if state.get("route") == "execute" and state.get("approval_payload_hash"):
        return "execute"
    return "reject"


def route_execute(state) -> str:
    if state.get("status") == "APPROVAL_MISMATCH" or state.get("route") == "stop":
        return "stop"
    return "verify"


def route_verify(state) -> str:
    verification = state.get("verification") or {}
    if verification.get("recovered") is True:
        return "recovered"
    # A 5-minute rate that has not moved yet is not evidence the fix failed.
    if verification.get("pending") is True:
        return "pending"
    return "rollback"
