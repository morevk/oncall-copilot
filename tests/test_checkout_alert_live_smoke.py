"""Live smoke of ``CHECKOUT_ALERT`` through the incident API.

Skipped unless ``ONCALL_LIVE_SMOKE=1``. The test arms ``paymentFailure`` to
``100%`` and does not post the alert until Prometheus shows the checkout
detect ratio above 5%. It sets the flag back to ``off`` on every exit,
including when an assertion fails.

The detect ratio is the Grafana ``CheckoutErrorRateHigh`` expression: frontend
422s over 422s plus successful payment transactions. Both series are the
queries ``verify_metrics`` already uses.

    ONCALL_LIVE_SMOKE=1 uv run pytest \
        tests/test_checkout_alert_live_smoke.py -v
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from oncall_contracts.flagd import current_variant, read_document, set_variant, write_document
from oncall_graph.nodes.remediate import CHECKOUT_FAILURE_QUERY, RECOVERY_QUERY
from oncall_graph.team_demo import CHECKOUT_ALERT

pytestmark = pytest.mark.skipif(
    os.environ.get("ONCALL_LIVE_SMOKE") != "1",
    reason="set ONCALL_LIVE_SMOKE=1 to post a team alert at a running incident API",
)

BASE_URL = os.environ.get("ONCALL_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090").rstrip("/")
DEADLINE_SECONDS = 180
VERIFY_DEADLINE_SECONDS = 360
SIGNAL_DEADLINE_SECONDS = 300
# CheckoutErrorRateHigh fires above this ratio. eval/faults/manifest.yaml.
DETECT_THRESHOLD = 0.05
DETECT_QUERY = f"{CHECKOUT_FAILURE_QUERY} / ({CHECKOUT_FAILURE_QUERY} + {RECOVERY_QUERY})"
FLAG = "paymentFailure"
ARMED = "100%"
DISARMED = "off"
# Every variant in deploy/flagd/demo.flagd.json. The smoke refuses to arm
# until this set is present, then selects the manifest fault variant.
FAULT_VARIANTS = {"10%", "25%", "50%", "75%", "90%", "100%", "off"}
EXPECTED_ARGS = {"flag": FLAG, "variant": DISARMED}
FINISHED = {"RECOVERED", "APPLIED"}
FLAG_PATH = Path(
    os.environ.get(
        "FLAGD_CONFIG",
        Path(__file__).resolve().parents[1] / "deploy" / "flagd" / "demo.flagd.json",
    )
)


def _variants() -> set[str]:
    document = read_document(FLAG_PATH)
    definition = document["flags"][FLAG]
    variants = definition.get("variants")
    assert isinstance(variants, dict), f"{FLAG} has no variants object"
    return set(variants)


def _require_fault_variants() -> None:
    present = _variants()
    missing = FAULT_VARIANTS - present
    if missing:
        raise AssertionError(f"{FLAG} is missing variants {sorted(missing)}")


def _set_flag(variant: str) -> None:
    """Write one default variant and read it back."""
    if variant not in _variants():
        raise AssertionError(f"{FLAG} has no variant {variant!r}")
    document = read_document(FLAG_PATH)
    updated, changed = set_variant(document, FLAG, variant)
    if changed:
        write_document(FLAG_PATH, updated)
    actual = current_variant(read_document(FLAG_PATH), FLAG)
    if actual != variant:
        raise AssertionError(f"{FLAG} is {actual!r}, expected {variant!r}")


@pytest.fixture
def payment_failure_armed() -> Iterator[None]:
    """Require the flag's variants, arm the fault, then disarm on every exit."""
    _require_fault_variants()
    _set_flag(ARMED)
    try:
        yield
    finally:
        _set_flag(DISARMED)


def _request(method: str, path: str, body: dict | None = None, *, base: str = BASE_URL) -> dict:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        raise AssertionError(f"{method} {path} returned {exc.code}: {detail}") from exc


def _detect_ratio() -> float | None:
    """The checkout detect sample, or None when Prometheus has no number yet."""
    path = "/api/v1/query?" + urllib.parse.urlencode({"query": DETECT_QUERY})
    payload = _request("GET", path, base=PROMETHEUS_URL)
    if payload.get("status") != "success":
        raise AssertionError(f"prometheus query failed: {payload.get('error')}")
    result = (payload.get("data") or {}).get("result") or []
    if not result:
        return None
    raw = result[0].get("value", [None, None])[1]
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(value):
        return None
    return value


def _wait_for_checkout_signal() -> float:
    """Do not post the alert until the fault is visible on the detect query."""
    deadline = time.monotonic() + SIGNAL_DEADLINE_SECONDS
    latest: float | None = None
    while time.monotonic() < deadline:
        latest = _detect_ratio()
        if latest is not None and latest > DETECT_THRESHOLD:
            return latest
        time.sleep(10)
    raise AssertionError(
        f"checkout detect ratio is {latest}, need > {DETECT_THRESHOLD} "
        f"within {SIGNAL_DEADLINE_SECONDS}s of arming {FLAG}={ARMED}"
    )


def _wait(incident_id: str, seconds: int, *, terminal: bool) -> dict:
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


def test_checkout_alert_reaches_approval(payment_failure_armed: None) -> None:
    ratio = _wait_for_checkout_signal()
    assert ratio > DETECT_THRESHOLD

    alert = {**CHECKOUT_ALERT, "startsAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    incident_id = f"smoke-checkout-{uuid.uuid4().hex[:8]}"
    started = _request(
        "POST",
        f"/alerts?incident_id={incident_id}&config_id=c2_team",
        alert,
    )
    assert started["incident_id"] == incident_id
    assert started["config_id"] == "c2_team"
    assert started["tool_budget"] == 40
    assert started["alert"]["labels"]["alertname"] == "CheckoutErrorRateHigh"

    current = _wait(incident_id, DEADLINE_SECONDS, terminal=False)
    assert current["status"] == "AWAITING_APPROVAL", current.get("error")
    proposed = current["state"]["proposed_action"]
    assert proposed["action"] == "set_flag"
    assert proposed["args"] == EXPECTED_ARGS
    assert current["approval"]["command"] == {"action": "set_flag", "args": EXPECTED_ARGS}
    assert current["approval"]["payload_hash"]
    assert int(current["state"]["tool_calls"]) > 0

    _approve(incident_id, current["approval"]["payload_hash"])
    finished = _wait(incident_id, VERIFY_DEADLINE_SECONDS, terminal=True)
    assert finished["status"] in FINISHED, (finished.get("status"), finished.get("error"))
    assert not finished.get("error")
