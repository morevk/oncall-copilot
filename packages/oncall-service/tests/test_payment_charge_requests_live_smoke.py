"""Live smoke of the team demo alerts through the incident API.

Skipped unless ``ONCALL_LIVE_SMOKE=1``. Each test posts one alert from
``team_demo`` to a running oncall-service, which calls OpenRouter and reads
the lab. It does not arm or disarm a fault.

Each alert approves the proposed ``set_flag`` and waits until verification
finishes. For ``paymentFailure``, ``RECOVERED`` means the payment-success rate
moved and ``APPLIED`` means the five-minute window ended on a quiet checkout.
Checkout 422s still arriving after that window roll the flag back. For
``paymentUnreachable``, ``RECOVERED`` means the payment server Charge rate
rose above zero. ``APPLIED`` means the window ended with both Charge rates at
zero. Checkout Charge client errors still above zero while that server rate
is still zero roll the flag back. A rollback leaves these statuses.

``paymentFailure`` should be armed for the charge and checkout alerts.
``paymentUnreachable`` should be armed, and ``paymentFailure`` disarmed, for
the unavailable alert.

    ONCALL_LIVE_SMOKE=1 uv run pytest \
        packages/oncall-service/tests/test_payment_charge_requests_live_smoke.py -v
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

import pytest
from oncall_graph.team_demo import (
    CHECKOUT_ALERT,
    PAYMENT_CHARGE_REQUESTS_ALERT,
    PAYMENT_UNAVAILABLE_ALERT,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("ONCALL_LIVE_SMOKE") != "1",
    reason="set ONCALL_LIVE_SMOKE=1 to post a team alert at a running incident API",
)

BASE_URL = os.environ.get("ONCALL_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
DEADLINE_SECONDS = 180
# The last recovery poll waits out five minutes. A success can return sooner.
VERIFY_DEADLINE_SECONDS = 360
FINISHED = {"RECOVERED", "APPLIED"}


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        raise AssertionError(f"{method} {path} returned {exc.code}: {detail}") from exc


def _wait(incident_id: str, seconds: int, *, terminal: bool) -> dict:
    """``terminal`` waits out verification. Approval is not terminal."""
    deadline = time.monotonic() + seconds
    current = _request("GET", f"/incidents/{incident_id}")
    while time.monotonic() < deadline:
        done = (
            current["terminal"]
            if terminal
            else current["terminal"] or current["status"] == "AWAITING_APPROVAL"
        )
        if done:
            return current
        time.sleep(2)
        current = _request("GET", f"/incidents/{incident_id}")
    raise AssertionError(f"{incident_id} still {current['status']} after {seconds}s")


def _approve(incident_id: str, payload_hash: str) -> None:
    """Retry while the investigation task is still marked running."""
    body = {"decision": "approve", "payload_hash": payload_hash}
    deadline = time.monotonic() + 10
    last = ""
    while time.monotonic() < deadline:
        try:
            _request("POST", f"/incidents/{incident_id}/approve", body)
            return
        except AssertionError as exc:
            last = str(exc)
            if "409" not in last or "already running" not in last:
                raise
            time.sleep(0.5)
    raise AssertionError(last)


def _run_until_approval(alert: dict, incident_prefix: str, flag: str, *, approve: bool) -> None:
    incident_id = f"{incident_prefix}-{uuid.uuid4().hex[:8]}"
    started = _request(
        "POST",
        f"/alerts?incident_id={incident_id}&config_id=c2_team",
        alert,
    )
    assert started["incident_id"] == incident_id
    assert started["config_id"] == "c2_team"
    assert started["tool_budget"] == 40
    assert started["alert"]["labels"]["alertname"] == alert["labels"]["alertname"]

    current = _wait(incident_id, DEADLINE_SECONDS, terminal=False)

    expected = {"flag": flag, "variant": "off"}
    assert current["status"] == "AWAITING_APPROVAL", current.get("error")
    proposed = current["state"]["proposed_action"]
    assert proposed["action"] == "set_flag"
    assert proposed["args"] == expected
    assert current["approval"]["command"] == {"action": "set_flag", "args": expected}
    assert current["approval"]["payload_hash"]
    assert int(current["state"]["tool_calls"]) > 0
    if not approve:
        return

    _approve(incident_id, current["approval"]["payload_hash"])
    finished = _wait(incident_id, VERIFY_DEADLINE_SECONDS, terminal=True)
    assert finished["status"] in FINISHED, (finished.get("status"), finished.get("error"))
    assert not finished.get("error")


def test_payment_charge_requests_alert_reaches_approval() -> None:
    _run_until_approval(
        PAYMENT_CHARGE_REQUESTS_ALERT, "smoke-payment-charge", "paymentFailure", approve=True
    )


def test_payment_unavailable_alert_reaches_approval() -> None:
    _run_until_approval(
        PAYMENT_UNAVAILABLE_ALERT,
        "smoke-payment-unavailable",
        "paymentUnreachable",
        approve=True,
    )


def test_checkout_alert_reaches_approval() -> None:
    _run_until_approval(CHECKOUT_ALERT, "smoke-checkout", "paymentFailure", approve=True)
