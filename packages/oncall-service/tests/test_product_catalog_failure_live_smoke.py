"""Live smoke of a productCatalogFailure alert through the incident API.

Skipped unless ``ONCALL_LIVE_SMOKE=1``. Posts one alert to a running
oncall-service. Before the alert, the test sets ``productCatalogFailure`` to
its fault variant ``on``. After the test, including when an assertion fails,
it sets that flag back to ``off``.

``paymentFailure`` should already be ``off``. The service shop cannot measure
the catalog flag, so scripted verification fails. The approved ``set_flag`` is
rolled back to ``on`` and the run replans. Replan stops at a second approval,
or escalates when the tool budget runs out before that proposal. This test
approves only the first proposal. The post-condition is what leaves the flag
disarmed.

    ONCALL_LIVE_SMOKE=1 uv run pytest \
        packages/oncall-service/tests/test_product_catalog_failure_live_smoke.py -v
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from oncall_contracts.flagd import current_variant, read_document, set_variant, write_document

pytestmark = pytest.mark.skipif(
    os.environ.get("ONCALL_LIVE_SMOKE") != "1",
    reason="set ONCALL_LIVE_SMOKE=1 to post a team alert at a running incident API",
)

BASE_URL = os.environ.get("ONCALL_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
DEADLINE_SECONDS = 180
CATALOG_ALERT = {
    "status": "firing",
    "labels": {"alertname": "ProductCatalogFailure", "service": "product-catalog"},
    "annotations": {"summary": "product pages fail to load for specific products"},
    "startsAt": "2026-11-10T10:50:00Z",
}
FLAG = "productCatalogFailure"
ARMED = "on"
DISARMED = "off"
EXPECTED_ARGS = {"flag": FLAG, "variant": DISARMED}
FLAG_PATH = Path(
    os.environ.get(
        "FLAGD_CONFIG",
        Path(__file__).resolve().parents[3] / "deploy" / "flagd" / "demo.flagd.json",
    )
)


def _set_flag(variant: str) -> None:
    """Write one default variant and read it back."""
    document = read_document(FLAG_PATH)
    updated, changed = set_variant(document, FLAG, variant)
    if changed:
        write_document(FLAG_PATH, updated)
    actual = current_variant(read_document(FLAG_PATH), FLAG)
    if actual != variant:
        raise AssertionError(f"{FLAG} is {actual!r}, expected {variant!r}")


@pytest.fixture
def catalog_flag_armed() -> Iterator[None]:
    """Arm the catalog fault, then disarm it on every exit path."""
    _set_flag(ARMED)
    try:
        yield
    finally:
        _set_flag(DISARMED)


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


def _wait_for_approval(incident_id: str) -> dict:
    deadline = time.monotonic() + DEADLINE_SECONDS
    current = _request("GET", f"/incidents/{incident_id}")
    while time.monotonic() < deadline:
        if current["terminal"] or current["status"] == "AWAITING_APPROVAL":
            return current
        time.sleep(2)
        current = _request("GET", f"/incidents/{incident_id}")
    raise AssertionError(f"{incident_id} still {current['status']} after {DEADLINE_SECONDS}s")


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


def _texts(incident: dict) -> list[str]:
    transcript = (incident.get("state") or {}).get("transcript") or []
    return [str(item.get("text") or "") for item in transcript]


def _wait_for_replan(incident_id: str) -> dict:
    """The post-rollback stop: a second approval, or a terminal budget escalation."""
    deadline = time.monotonic() + DEADLINE_SECONDS
    current = _request("GET", f"/incidents/{incident_id}")
    while time.monotonic() < deadline:
        texts = _texts(current)
        replanned = "rollback" in texts and "replan" in texts
        stopped = current["terminal"] or current["status"] == "AWAITING_APPROVAL"
        if current.get("error"):
            return current
        if replanned and stopped:
            return current
        time.sleep(2)
        current = _request("GET", f"/incidents/{incident_id}")
    raise AssertionError(f"{incident_id} still {current['status']} after rollback")


def test_product_catalog_failure_rolls_back_and_replans(catalog_flag_armed: None) -> None:
    incident_id = f"smoke-product-catalog-{uuid.uuid4().hex[:8]}"
    started = _request(
        "POST",
        f"/alerts?incident_id={incident_id}&config_id=c2_team",
        CATALOG_ALERT,
    )
    assert started["incident_id"] == incident_id
    assert started["config_id"] == "c2_team"
    assert started["tool_budget"] == 40
    assert started["alert"]["labels"]["alertname"] == "ProductCatalogFailure"

    current = _wait_for_approval(incident_id)
    assert current["status"] == "AWAITING_APPROVAL", current.get("error")
    proposed = current["state"]["proposed_action"]
    assert proposed["action"] == "set_flag"
    assert proposed["args"] == EXPECTED_ARGS
    assert current["approval"]["command"] == {"action": "set_flag", "args": EXPECTED_ARGS}
    first_hash = current["approval"]["payload_hash"]
    assert first_hash
    assert int(current["state"]["tool_calls"]) > 0

    _approve(incident_id, first_hash)
    finished = _wait_for_replan(incident_id)

    # Scripted verify cannot measure the catalog flag, so the approved fix is
    # rolled back and the run replans. A lighter re-investigation stops at a
    # second approval. One that spends the tool budget escalates instead.
    assert not finished.get("error")
    texts = _texts(finished)
    assert "executed set_flag" in texts
    assert "verify recovered=False" in texts
    rollback_at = texts.index("rollback")
    replan_at = texts.index("replan")
    assert rollback_at < replan_at
    status = finished["status"]
    assert status in {"AWAITING_APPROVAL", "ESCALATED"}, (status, finished.get("error"))
    if status == "AWAITING_APPROVAL":
        again = finished["state"]["proposed_action"]
        assert again["action"] == "set_flag"
        assert again["args"] == EXPECTED_ARGS
        return

    assert "triage budget exhausted" in texts[replan_at:]
    run = finished["system_run"]
    assert run["verified_recovery"] is False
    assert run["rolled_back"] is True
    assert run["executed_actions"] == [
        {"action": "set_flag", "args": EXPECTED_ARGS},
        {"action": "rollback_flag", "args": {"flag": FLAG, "variant": ARMED}},
    ]
