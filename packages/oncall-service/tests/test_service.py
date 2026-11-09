import sqlite3
import time
from pathlib import Path
from typing import ClassVar

from fastapi.testclient import TestClient
from oncall_graph.build import tools_requested
from oncall_service.main import create_app


def _wait_for_status(client: TestClient, incident_id: str, expected: str) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = client.get(f"/incidents/{incident_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] == expected:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"incident {incident_id} did not reach {expected}")


def test_health_and_console_are_served():
    with TestClient(create_app(demo_delay=0)) as client:
        health = client.get("/healthz")
        capabilities = client.get("/api/evaluation-capabilities")
        page = client.get("/")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert capabilities.json() == {
        "configurations": ["c1_single", "c2_team"],
        "approval": "exact-action-payload-hash",
        "system_run_contract": "v1",
    }
    assert page.status_code == 200
    assert "Incident Console" in page.text
    assert "No incident selected" in page.text
    assert 'id="topology-select"' in page.text
    assert "C2 team" in page.text


def test_demo_streams_to_approval_and_exact_hash_recovers():
    with TestClient(create_app(demo_delay=0)) as client:
        started = client.post("/api/demo")
        assert started.status_code == 202
        incident_id = started.json()["incident_id"]
        waiting = _wait_for_status(client, incident_id, "AWAITING_APPROVAL")

        assert waiting["state"]["hypotheses"][0]["service"] == "payment"
        assert len(waiting["state"]["evidence"]) == 2
        assert waiting["approval"]["payload_hash"]
        assert waiting["events"][-1]["type"] == "approval.requested"

        stale = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "approve", "payload_hash": "stale"},
        )
        assert stale.status_code == 409

        approved = client.post(
            f"/incidents/{incident_id}/approve",
            json={
                "decision": "approve",
                "payload_hash": waiting["approval"]["payload_hash"],
            },
        )
        assert approved.status_code == 200
        recovered = _wait_for_status(client, incident_id, "RECOVERED")

    assert recovered["terminal"]
    assert recovered["state"]["verification"]["recovered"] is True
    assert recovered["system_run"]["verified_recovery"] is True
    assert recovered["events"][-1]["type"] == "verification.recovered"


def test_demo_rejection_is_terminal_and_executes_nothing():
    with TestClient(create_app(demo_delay=0)) as client:
        incident_id = client.post("/api/demo").json()["incident_id"]
        _wait_for_status(client, incident_id, "AWAITING_APPROVAL")
        response = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "reject"},
        )
        assert response.status_code == 200
        rejected = _wait_for_status(client, incident_id, "REJECTED")

    assert rejected["terminal"]
    assert rejected["approval"] is None
    assert rejected["system_run"] is None


def test_alert_contract_rejects_incomplete_payload_before_agent_start():
    with TestClient(create_app(demo_delay=0)) as client:
        response = client.post("/alerts", json={"status": "firing"})

    assert response.status_code == 422
    assert "missing fields" in response.json()["detail"]


def test_incident_feed_records_creation_and_state_updates():
    app = create_app(demo_delay=0)
    with TestClient(app) as client:
        response = client.post("/api/demo")
        assert response.status_code == 202
        incident_id = response.json()["incident_id"]
        waiting = _wait_for_status(client, incident_id, "AWAITING_APPROVAL")

    feed_events = app.state.incidents.feed_events
    assert feed_events[0]["type"] == "incident.created"
    assert feed_events[0]["incident"]["incident_id"] == incident_id
    assert feed_events[-1]["type"] == "approval.requested"
    assert feed_events[-1]["incident"]["status"] == waiting["status"]


def test_completed_incident_and_event_feed_survive_a_restart(tmp_path: Path):
    database = tmp_path / "incidents.sqlite3"
    with TestClient(create_app(demo_delay=0, database_path=database)) as client:
        incident_id = client.post("/api/demo").json()["incident_id"]
        waiting = _wait_for_status(client, incident_id, "AWAITING_APPROVAL")
        client.post(
            f"/incidents/{incident_id}/approve",
            json={
                "decision": "approve",
                "payload_hash": waiting["approval"]["payload_hash"],
            },
        )
        recovered = _wait_for_status(client, incident_id, "RECOVERED")

    restarted = create_app(demo_delay=0, database_path=database)
    with TestClient(restarted) as client:
        restored = client.get(f"/incidents/{incident_id}")
        listed = client.get("/api/incidents")

    assert database.is_file()
    assert restored.status_code == 200
    assert restored.json() == recovered
    assert listed.json() == [recovered]
    assert restarted.state.incidents.feed_events[-1]["incident"] == recovered
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM incidents").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM incident_events").fetchone()[0]
            == recovered["event_count"]
        )
        assert (
            connection.execute("SELECT count(*) FROM feed_events").fetchone()[0]
            == recovered["event_count"]
        )


def test_unfinished_incident_fails_closed_after_a_restart(tmp_path: Path):
    database = tmp_path / "incidents.sqlite3"
    with TestClient(create_app(demo_delay=0, database_path=database)) as client:
        incident_id = client.post("/api/demo").json()["incident_id"]
        _wait_for_status(client, incident_id, "AWAITING_APPROVAL")

    with TestClient(create_app(demo_delay=0, database_path=database)) as client:
        restored = client.get(f"/incidents/{incident_id}").json()

    assert restored["status"] == "ESCALATED"
    assert restored["terminal"] is True
    assert restored["approval"] is None
    assert restored["events"][-1]["type"] == "incident.interrupted"
    assert "not resumed" in restored["error"]


def test_configured_frontend_origin_can_post_alerts():
    app = create_app(demo_delay=0, cors_origins=("http://localhost:5173",))
    with TestClient(app) as client:
        response = client.options(
            "/alerts",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


ALERT = {
    "status": "firing",
    "labels": {
        "alertname": "CheckoutErrorRateHigh",
        "service": "checkout",
        "severity": "critical",
    },
    "annotations": {"summary": "Checkout failures exceed 5%"},
    "startsAt": "2026-11-02T00:00:00Z",
}


def _install_runners(monkeypatch, *, team_pauses: bool = False):
    """Stand in for both topologies so a test never builds an OpenRouter client."""

    class Shop:
        def __init__(self):
            self.writes = []

    def runner(name: str, pauses: bool):
        class Runner:
            built: ClassVar[list] = []

            def __init__(self, shop=None, tool_budget=10, **kwargs):
                self.shop = shop
                self.tool_budget = tool_budget
                self.extra = kwargs
                self.resumes: list = []
                self.built.append(self)

            def run(self, alert="", *, incident_id="incident", resume=None):
                self.resumes.append(resume)
                if resume is None and pauses:
                    return (
                        {
                            "__interrupt__": [
                                {
                                    "command": {"action": "set_flag", "args": {}},
                                    "expected_effect": "recover",
                                    "payload_hash": "hash-1",
                                }
                            ],
                            "plan": {"stage": "act"},
                        },
                        None,
                    )
                status = "RECOVERED" if resume is not None else "ESCALATED"
                return ({"status": status, "stage": "communicate"}, None)

        Runner.__name__ = name
        return Runner

    solo = runner("Solo", pauses=False)
    team = runner("Team", pauses=team_pauses)
    monkeypatch.setattr("oncall_service.main.LiveShop", Shop)
    monkeypatch.setattr("oncall_service.main.Solo", solo)
    monkeypatch.setattr("oncall_service.main.Team", team)
    return solo, team, Shop


def test_omitted_budget_starts_the_solo_runner(monkeypatch):
    solo, team, shop = _install_runners(monkeypatch)
    with TestClient(create_app(demo_delay=0)) as client:
        started = client.post("/alerts", json=ALERT)
        assert started.status_code == 202
        incident_id = started.json()["incident_id"]
        finished = _wait_for_status(client, incident_id, "ESCALATED")

    assert finished["config_id"] == "c1_single"
    assert finished["tool_budget"] == 10
    assert len(solo.built) == 1
    assert isinstance(solo.built[0].shop, shop)
    assert solo.built[0].tool_budget == 10
    assert team.built == []


def test_team_alert_uses_the_live_budget_and_resumes_the_same_runner(monkeypatch):
    solo, team, shop = _install_runners(monkeypatch, team_pauses=True)
    with TestClient(create_app(demo_delay=0)) as client:
        started = client.post("/alerts?config_id=c2_team", json=ALERT)
        assert started.status_code == 202
        body = started.json()
        assert body["config_id"] == "c2_team"
        assert body["tool_budget"] == 40
        incident_id = body["incident_id"]
        waiting = _wait_for_status(client, incident_id, "AWAITING_APPROVAL")
        approved = client.post(
            f"/incidents/{incident_id}/approve",
            json={"decision": "approve", "payload_hash": waiting["approval"]["payload_hash"]},
        )
        assert approved.status_code == 200
        recovered = _wait_for_status(client, incident_id, "RECOVERED")

    assert recovered["terminal"]
    assert len(team.built) == 1
    assert isinstance(team.built[0].shop, shop)
    assert team.built[0].tool_budget == 40
    assert set(team.built[0].extra) == {"store"}
    assert team.built[0].extra["store"].__class__.__name__ == "LessonStore"
    assert team.built[0].resumes == [
        None,
        {"decision": "approve", "payload_hash": "hash-1"},
    ]
    assert solo.built == []


def test_explicit_team_budget_is_kept(monkeypatch):
    _solo, team, _shop = _install_runners(monkeypatch)
    with TestClient(create_app(demo_delay=0)) as client:
        started = client.post("/alerts?config_id=c2_team&tool_budget=12", json=ALERT)
        assert started.status_code == 202
        incident_id = started.json()["incident_id"]
        finished = _wait_for_status(client, incident_id, "ESCALATED")

    assert finished["tool_budget"] == 12
    assert team.built[0].tool_budget == 12


def test_unimplemented_topology_is_rejected_before_a_runner_starts(monkeypatch):
    solo, team, _shop = _install_runners(monkeypatch)
    with TestClient(create_app(demo_delay=0)) as client:
        response = client.post("/alerts?config_id=c0_workflow", json=ALERT)

    assert response.status_code == 422
    assert response.json()["detail"] == "only c1_single and c2_team are implemented"
    assert solo.built == []
    assert team.built == []


def test_tools_connect_only_when_the_transport_is_configured(monkeypatch):
    """The image check sets no MCP_TRANSPORT. Connecting there exits the process."""
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    assert tools_requested() is False
    monkeypatch.setenv("MCP_TRANSPORT", "   ")
    assert tools_requested() is False

    opened = []

    def explode():
        opened.append("open")
        raise AssertionError("open_tools was called")

    monkeypatch.setattr("oncall_service.main.open_tools", explode)
    with TestClient(create_app(demo_delay=0, connect_tools=tools_requested())) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
    assert opened == []

    monkeypatch.setenv("MCP_TRANSPORT", "http")
    assert tools_requested() is True


def test_a_connected_app_passes_the_registry_and_does_not_build_live_shop(monkeypatch):
    class _Registry:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    registry = _Registry()

    def explode():
        raise AssertionError("LiveShop was constructed")

    solo, team, live_shop = _install_runners(monkeypatch)
    monkeypatch.setattr("oncall_service.main.LiveShop", explode)
    monkeypatch.setattr("oncall_service.main.open_tools", lambda: registry)
    with TestClient(create_app(demo_delay=0, connect_tools=True)) as client:
        started = client.post("/alerts", json=ALERT)
        assert started.status_code == 202
        _wait_for_status(client, started.json()["incident_id"], "ESCALATED")
        team_started = client.post("/alerts?config_id=c2_team", json=ALERT)
        assert team_started.status_code == 202
        _wait_for_status(client, team_started.json()["incident_id"], "ESCALATED")

    assert registry.closed
    assert solo.built[0].extra["tools"] is registry
    assert not isinstance(solo.built[0].shop, live_shop)
    assert team.built[0].extra["tools"] is registry
    assert not isinstance(team.built[0].shop, live_shop)
