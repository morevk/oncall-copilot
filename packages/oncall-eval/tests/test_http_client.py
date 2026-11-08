from urllib.parse import parse_qs, urlparse

import pytest
from oncall_eval.http_client import IncidentApiClient, IncidentApiError


def _system_run() -> dict:
    action = {
        "action": "set_flag",
        "args": {"flag": "paymentFailure", "variant": "off"},
    }
    return {
        "root_cause_services": ["payment"],
        "proposed_action": action,
        "executed_actions": [action],
        "tool_calls": 4,
        "tokens": 100,
        "usd": 0.01,
        "verified_recovery": True,
        "rolled_back": False,
        "model_ids": {"solo": "test-model"},
    }


def _alert() -> dict:
    return {
        "status": "firing",
        "labels": {"alertname": "CheckoutErrorRateHigh", "service": "checkout"},
        "annotations": {"summary": "Checkout failures"},
        "startsAt": "2026-11-02T00:00:00Z",
    }


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_client_submits_alert_polls_approves_exact_hash_and_returns_evidence():
    expected = {
        "action": "set_flag",
        "args": {"flag": "paymentFailure", "variant": "off"},
    }
    responses = iter(
        [
            {"status": "RUNNING"},
            {
                "status": "RUNNING",
                "approval": {"payload_hash": "abc123", "command": expected},
            },
            {"status": "RECOVERED", "system_run": _system_run()},
        ]
    )
    requests = []

    def transport(method: str, url: str, body, timeout: float):
        requests.append((method, url, body, timeout))
        return next(responses)

    clock = FakeClock()
    client = IncidentApiClient(
        "http://incident:8000/",
        transport=transport,
        clock=clock,
        sleep=clock.sleep,
    )

    result = client.run(
        _alert(),  # type: ignore[arg-type]
        incident_id="eval/run 1",
        config_id="c1_single",
        tool_budget=5,
        expected_action=expected,  # type: ignore[arg-type]
    )

    assert result == _system_run()
    assert requests[0][0] == "POST"
    assert requests[0][2] == _alert()
    query = parse_qs(urlparse(requests[0][1]).query)
    assert query == {
        "incident_id": ["eval/run 1"],
        "config_id": ["c1_single"],
        "tool_budget": ["5"],
    }
    assert requests[1][0:3] == (
        "GET",
        "http://incident:8000/incidents/eval%2Frun%201",
        None,
    )
    assert requests[2][0:3] == (
        "POST",
        "http://incident:8000/incidents/eval%2Frun%201/approve",
        {"decision": "approve", "payload_hash": "abc123"},
    )


def test_client_never_resubmits_a_decision_for_the_same_approval_hash():
    requests = []

    def transport(method: str, url: str, body, timeout: float):
        requests.append((method, url, body, timeout))
        return {"approval": {"payload_hash": "same"}}

    clock = FakeClock()
    client = IncidentApiClient(
        run_timeout_seconds=2,
        poll_interval_seconds=1,
        transport=transport,
        clock=clock,
        sleep=clock.sleep,
    )

    with pytest.raises(IncidentApiError, match="did not clear"):
        client.run(
            _alert(),
            incident_id="run",
            config_id="c1_single",
            tool_budget=5,
            expected_action=None,
        )  # type: ignore[arg-type]

    decision_requests = [request for request in requests if request[0] == "POST"]
    assert len(decision_requests) == 2  # alert submission plus one rejection


def test_client_times_out_nonterminal_incident():
    clock = FakeClock()
    client = IncidentApiClient(
        run_timeout_seconds=2,
        poll_interval_seconds=1,
        transport=lambda *_: {"status": "RUNNING"},
        clock=clock,
        sleep=clock.sleep,
    )

    with pytest.raises(IncidentApiError, match="did not terminate"):
        client.run(
            _alert(),
            incident_id="run",
            config_id="c1_single",
            tool_budget=5,
            expected_action=None,
        )  # type: ignore[arg-type]


def test_client_requires_terminal_system_evidence():
    client = IncidentApiClient(transport=lambda *_: {"status": "RECOVERED"})

    with pytest.raises(IncidentApiError, match="no system_run"):
        client.run(
            _alert(),
            incident_id="run",
            config_id="c1_single",
            tool_budget=5,
            expected_action=None,
        )  # type: ignore[arg-type]


def test_client_rejects_a_proposal_that_does_not_exactly_match_expected_action():
    wrong = {"action": "set_flag", "args": {"flag": "adFailure", "variant": "off"}}
    terminal = _system_run() | {
        "proposed_action": wrong,
        "executed_actions": [],
        "verified_recovery": False,
    }
    responses = iter(
        [
            {"approval": {"payload_hash": "wrong-hash", "command": wrong}},
            {"status": "REJECTED", "system_run": terminal},
        ]
    )
    requests = []

    def transport(method: str, url: str, body, timeout: float):
        requests.append((method, url, body, timeout))
        return next(responses)

    client = IncidentApiClient(transport=transport)
    expected = {
        "action": "set_flag",
        "args": {"flag": "paymentFailure", "variant": "off"},
    }

    result = client.run(
        _alert(),
        incident_id="safe-reject",
        config_id="c1_single",
        tool_budget=5,
        expected_action=expected,  # type: ignore[arg-type]
    )

    assert result["executed_actions"] == []
    assert requests[1][2] == {"decision": "reject", "payload_hash": None}


def test_client_preflight_requires_healthy_incident_api():
    def healthy_transport(_method, url, _body, _timeout):
        if url.endswith("/healthz"):
            return {"status": "ok"}
        return {
            "configurations": ["c1_single"],
            "approval": "exact-action-payload-hash",
            "system_run_contract": "v1",
        }

    healthy = IncidentApiClient(transport=healthy_transport)
    unhealthy = IncidentApiClient(transport=lambda *_: {"status": "starting"})

    healthy.preflight("c1_single")
    with pytest.raises(IncidentApiError, match="health check"):
        unhealthy.preflight("c1_single")


def test_client_preflight_refuses_a_topology_the_service_does_not_advertise():
    def transport(_method, url, _body, _timeout):
        if url.endswith("/healthz"):
            return {"status": "ok"}
        return {
            "configurations": ["c1_single"],
            "approval": "exact-action-payload-hash",
            "system_run_contract": "v1",
        }

    client = IncidentApiClient(transport=transport)

    with pytest.raises(IncidentApiError, match="c2_team"):
        client.preflight("c2_team")
