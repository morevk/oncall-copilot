"""The lab arms one manifest flag and pages the agent with the symptom only."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from oncall_eval.batch import BatchEvaluationSummary
from oncall_eval.execution import EvaluationSettings
from oncall_eval.judge import JudgeSettings
from oncall_lab.batch_jobs import BatchEvaluationService
from oncall_lab.catalog import CatalogError, load_catalog
from oncall_lab.evaluation_jobs import EvaluationJobService
from oncall_lab.main import create_app
from oncall_lab.paging import notification
from oncall_lab.services import ServiceCheck, service_checks

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "eval/faults/manifest.yaml"
SHOP_FLAGS = ROOT / "deploy/flagd/demo.flagd.json"

FORBIDDEN = ("payment", "paymentFailure", "set_flag", "root_cause", "100%")


def _flags(tmp_path: Path) -> Path:
    path = tmp_path / "demo.flagd.json"
    path.write_text(
        json.dumps(
            {
                "flags": {
                    "paymentFailure": {
                        "defaultVariant": "off",
                        "variants": {"off": 0, "100%": 1},
                    },
                    "adFailure": {
                        "defaultVariant": "off",
                        "variants": {"off": False, "on": True},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _client(
    tmp_path: Path,
    poster,
    flag_path: Path | None = None,
    manifest: Path = MANIFEST,
    symptoms=None,
):
    calls: list[tuple[str, dict]] = []

    def record(url: str, body: dict) -> dict:
        calls.append((url, body))
        return poster(url, body)

    app = create_app(
        manifest_path=manifest,
        flag_path=flag_path or _flags(tmp_path),
        alerts_url="http://oncall.test/alerts",
        console_url="http://localhost:8000/",
        evaluation_url="http://localhost:8030/",
        poster=record,
        clock=lambda: "2026-11-03T12:00:00Z",
        symptoms=symptoms,
    )
    return TestClient(app), calls


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _strings(child)]
    return []


def test_lab_page_offers_the_fault_and_keeps_ground_truth_on_this_service(tmp_path):
    client, _calls = _client(tmp_path, lambda _url, _body: {})
    with client:
        page = client.get("/")
        faults = client.get("/api/faults")
        script = client.get("/assets/app.js")

    assert page.status_code == 200
    assert "On-Call Copilot · Control Center" in page.text
    assert "Control center" in page.text
    assert "Incidents" in page.text
    assert "Evaluations" in page.text
    assert script.status_code == 200
    assert "Arm fault" in script.text
    assert "Page the agent now" in script.text
    assert "Turns this fault on in the live demo" in script.text
    assert "no investigation starts until you click Page the agent now" in script.text
    assert "Restores this fault's healthy baseline" in script.text
    assert "Sends only the visible symptom" in script.text
    assert "help-tip" in script.text
    assert "Ground truth" in script.text
    assert "Evaluation runner" in page.text
    assert "Run readiness check" in page.text
    assert "Run deterministic evaluation" in page.text
    assert "Evaluate a persisted incident" in page.text
    assert "Evaluate saved incident" in page.text
    assert "Evaluate all incidents" in page.text
    assert "Evaluate all pending incidents" in page.text
    assert "Re-evaluate incidents that already have both scores" in page.text
    assert "INJECT LIVE FAULTS" in page.text
    assert "/api/evaluation-jobs/capabilities" in script.text
    assert "/api/evaluation-jobs" in script.text
    assert "/api/saved-incidents" in script.text
    assert "/api/retrospective-evaluations" in script.text
    assert "/api/batch-evaluations" in script.text
    assert "EventSource" in script.text
    assert "Services" in page.text
    assert "Refresh" in page.text
    assert page.text.index("Evaluation runner") < page.text.index("Fault catalog")
    assert page.text.index("Fault catalog") < page.text.index("Services")
    assert "/api/services" in script.text

    payload = faults.json()
    assert payload["topology"] == "c1_single"
    assert payload["console_url"] == "http://localhost:8000/"
    assert payload["evaluation_url"] == "http://localhost:8030/"
    payment = payload["faults"][0]
    assert payment["fault_id"] == "payment-failure"
    assert payment["flag"] == "paymentFailure"
    assert payment["arm_variant"] == "100%"
    assert payment["disarm_variant"] == "off"
    assert payment["armed"] is False
    assert payment["root_cause_services"] == ["payment"]
    assert payment["can_page"] is True
    assert payment["symptom"]["alertname"] == "CheckoutErrorRateHigh"
    assert payment["symptom"]["service"] == "checkout"


def test_page_posts_the_symptom_and_a_repeat_keeps_the_same_fingerprint(tmp_path):
    client, calls = _client(
        tmp_path,
        lambda _url, _body: {"incidents": [{"incident_id": "alert-1"}]},
    )
    with client:
        first = client.post("/api/faults/payment-failure/page", json={"tool_budget": 4})
        second = client.post("/api/faults/payment-failure/page", json={})

    assert first.status_code == 200
    assert second.status_code == 200
    assert [url for url, _body in calls] == [
        "http://oncall.test/alerts?tool_budget=4",
        "http://oncall.test/alerts?tool_budget=10",
    ]
    first_alert, second_alert = (body for _url, body in calls)
    assert first_alert == second_alert
    assert first_alert["alerts"][0]["fingerprint"] == "checkout-error-rate-high"
    assert first_alert["alerts"][0]["labels"] == {
        "alertname": "CheckoutErrorRateHigh",
        "service": "checkout",
        "severity": "critical",
    }
    assert first.json()["incident_id"] == "alert-1"
    leaked = [text for text in _strings(first_alert) if any(token in text for token in FORBIDDEN)]
    assert leaked == []


def test_tool_budget_outside_the_agent_limit_is_rejected(tmp_path):
    client, calls = _client(tmp_path, lambda _url, _body: {})
    with client:
        response = client.post("/api/faults/payment-failure/page", json={"tool_budget": 0})

    assert response.status_code == 422
    assert calls == []


def test_arm_changes_only_the_named_flag_and_disarm_restores_it(tmp_path):
    flag_path = tmp_path / "demo.flagd.json"
    original = json.loads(SHOP_FLAGS.read_text(encoding="utf-8"))
    flag_path.write_text(json.dumps(original), encoding="utf-8")
    armed_once = json.loads(json.dumps(original))
    armed_once["flags"]["paymentFailure"]["defaultVariant"] = "100%"
    client, calls = _client(tmp_path, lambda _url, _body: {}, flag_path=flag_path)
    with client:
        armed = client.post("/api/faults/payment-failure/arm")
        during = json.loads(flag_path.read_text(encoding="utf-8"))
        unchanged = flag_path.read_bytes()
        again = client.post("/api/faults/payment-failure/arm")
        assert flag_path.read_bytes() == unchanged
        disarmed = client.post("/api/faults/payment-failure/disarm")

    assert armed.status_code == 200
    assert armed.json()["changed"] is True
    assert again.json()["changed"] is False
    assert disarmed.json()["changed"] is True
    assert calls == []
    assert during == armed_once
    assert json.loads(flag_path.read_text(encoding="utf-8")) == original


def test_arm_refuses_a_variant_the_live_file_does_not_have(tmp_path):
    flag_path = _flags(tmp_path)
    document = json.loads(flag_path.read_text(encoding="utf-8"))
    del document["flags"]["paymentFailure"]["variants"]["100%"]
    flag_path.write_text(json.dumps(document), encoding="utf-8")
    before = flag_path.read_bytes()
    client, _calls = _client(tmp_path, lambda _url, _body: {}, flag_path=flag_path)
    with client:
        response = client.post("/api/faults/payment-failure/arm")

    assert response.status_code == 409
    assert "100%" in response.json()["detail"]
    assert flag_path.read_bytes() == before


def test_unknown_fault_is_not_found(tmp_path):
    client, calls = _client(tmp_path, lambda _url, _body: {})
    with client:
        response = client.post("/api/faults/not-a-fault/arm")

    assert response.status_code == 404
    assert calls == []


def _gap_manifest(path: Path) -> None:
    path.write_text(
        """
faults:
  - fault_id: example-gap
    enable:
      action: set_flag
      args:
        flag: adFailure
        variant: "on"
    disable:
      action: set_flag
      args:
        flag: adFailure
        variant: "off"
    root_cause_services:
      - ad
    complexity: single
    correct_action:
      action: set_flag
      args:
        flag: adFailure
        variant: "off"
    detect:
      query: up
      operator: ">"
      threshold: 0
    recovery:
      query: up
      operator: ">"
      threshold: 0
""".strip(),
        encoding="utf-8",
    )


def test_a_published_symptom_must_name_a_manifest_fault(tmp_path):
    manifest = tmp_path / "manifest.yaml"
    _gap_manifest(manifest)
    with pytest.raises(CatalogError, match="payment-failure"):
        load_catalog(manifest)


def test_a_fault_without_a_published_symptom_cannot_page(tmp_path):
    manifest = tmp_path / "manifest.yaml"
    _gap_manifest(manifest)
    client, calls = _client(tmp_path, lambda _url, _body: {}, manifest=manifest, symptoms={})
    with client:
        faults = client.get("/api/faults")
        armed = client.post("/api/faults/example-gap/arm")
        paged = client.post("/api/faults/example-gap/page", json={})

    assert faults.json()["faults"][0]["can_page"] is False
    assert faults.json()["faults"][0]["root_cause_services"] == ["ad"]
    assert armed.status_code == 200
    assert armed.json()["fault"]["current_variant"] == "on"
    assert paged.status_code == 422
    assert calls == []


def test_service_checks_link_the_host_and_probe_the_network():
    host = {check.service_id: check for check in service_checks({})}
    assert host["prometheus"].href == "http://localhost:9090/"
    assert host["prometheus"].probe == "http://127.0.0.1:9090/-/ready"
    assert host["grafana"].href == "http://localhost:8080/grafana/"
    assert host["jaeger"].probe == "http://127.0.0.1:8080/jaeger/ui/"
    assert host["lab"].kind == "self"
    assert host["lab"].probe is None
    assert host["mcp-telemetry"].kind == "mcp"

    docker = {
        check.service_id: check
        for check in service_checks({"ONCALL_PROBE_PROMETHEUS": "http://prometheus:9090/-/ready"})
    }
    assert docker["prometheus"].href == "http://localhost:9090/"
    assert docker["prometheus"].probe == "http://prometheus:9090/-/ready"


def test_service_report_counts_a_listening_mcp_server_as_up(tmp_path):
    checks = (
        ServiceCheck(
            "prometheus",
            "Prometheus",
            "Shop consoles",
            "http://localhost:9090/",
            "http://prometheus/ready",
            "http",
        ),
        ServiceCheck(
            "grafana",
            "Grafana",
            "Shop consoles",
            "http://localhost:8080/grafana/",
            "http://grafana/api/health",
            "http",
        ),
        ServiceCheck("lab", "Lab", "On-call copilot", "http://localhost:8020/", None, "self"),
        ServiceCheck(
            "mcp-telemetry",
            "mcp-telemetry",
            "On-call copilot",
            "http://localhost:8101/mcp",
            "http://mcp/mcp",
            "mcp",
        ),
    )
    seen: list[str] = []

    def prober(url: str) -> int | None:
        seen.append(url)
        if url.endswith("/ready"):
            return 200
        if "grafana" in url:
            return None
        return 400

    app = create_app(
        manifest_path=MANIFEST,
        flag_path=_flags(tmp_path),
        services=checks,
        prober=prober,
    )
    with TestClient(app) as client:
        report = client.get("/api/services")

    assert report.status_code == 200
    body = report.json()
    assert [group["name"] for group in body["groups"]] == ["Shop consoles", "On-call copilot"]
    rows = {row["id"]: row for group in body["groups"] for row in group["services"]}
    assert rows["prometheus"]["status"] == "up"
    assert rows["prometheus"]["detail"] == "HTTP 200"
    assert rows["grafana"]["status"] == "down"
    assert rows["grafana"]["detail"] == "Unreachable"
    assert rows["lab"]["status"] == "up"
    assert rows["lab"]["detail"] == "This process"
    assert rows["mcp-telemetry"]["status"] == "up"
    assert "HTTP 400" in rows["mcp-telemetry"]["detail"]
    assert "prometheus:9090" not in report.text
    assert set(seen) == {"http://prometheus/ready", "http://grafana/api/health", "http://mcp/mcp"}


def test_notification_uses_only_the_symptom_fields():
    symptom = load_catalog(MANIFEST)[0].symptom
    assert symptom is not None
    body = notification(symptom, "2026-11-03T12:00:00Z")
    alert = body["alerts"][0]
    assert set(alert) == {"status", "fingerprint", "startsAt", "labels", "annotations"}
    assert set(alert["labels"]) == {"alertname", "service", "severity"}
    assert set(alert["annotations"]) == {"summary"}
    assert symptom.grafana_note not in _strings(body)


def test_saved_incident_can_be_scored_from_the_control_center(tmp_path: Path):
    action = {
        "action": "set_flag",
        "args": {"flag": "paymentFailure", "variant": "off"},
    }
    incident = {
        "incident_id": "saved-001",
        "status": "RECOVERED",
        "terminal": True,
        "mode": "hybrid-live",
        "config_id": "c1_single",
        "created_at": "2026-11-08T10:00:00Z",
        "updated_at": "2026-11-08T10:00:03Z",
        "alert": {"labels": {"alertname": "CheckoutErrorRateHigh", "service": "checkout"}},
        "system_run": {
            "root_cause_services": ["payment"],
            "proposed_action": action,
            "executed_actions": [action],
            "tool_calls": 2,
            "tokens": 0,
            "usd": 0.0,
            "verified_recovery": True,
            "rolled_back": False,
            "model_ids": {"solo": "fixture/demo"},
            "playbook_hash": None,
            "error": None,
        },
    }

    def getter(url: str):
        return [incident] if url.endswith("/api/incidents") else incident

    settings = EvaluationSettings(
        cases_dir=ROOT / "eval/dev",
        manifest_path=MANIFEST,
        flagd_config=tmp_path / "flags.json",
        prometheus_url="http://prometheus.test",
        api_url="http://oncall.test",
        artifacts_dir=tmp_path / "runs",
        lock_file=tmp_path / "live.lock",
        fail_under=1.0,
    )
    jobs = EvaluationJobService(tmp_path / "jobs.sqlite3", settings)
    app = create_app(
        manifest_path=MANIFEST,
        flag_path=_flags(tmp_path),
        alerts_url="http://oncall.test/alerts",
        evaluation_url="http://evaluation.test/",
        getter=getter,
        jobs=jobs,
    )
    with TestClient(app) as client:
        saved = client.get("/api/saved-incidents")
        cases = client.get("/api/evaluation-cases")
        scored = client.post(
            "/api/retrospective-evaluations",
            json={"incident_id": "saved-001", "case_id": "dev-001"},
        )

    assert saved.json()["incidents"][0]["eligible"] is True
    assert cases.json()["cases"] == [
        {"case_id": "dev-001", "fault_id": "payment-failure", "complexity": "single"}
    ]
    assert scored.status_code == 201
    result = scored.json()
    assert result["passed"] is False
    assert result["failure_reasons"] == ["system recovery claim was not independently observed"]
    assert result["report_url"].startswith("http://evaluation.test?run=retrospective-")
    assert (tmp_path / "runs" / f"{result['run_id']}.json").is_file()


def test_batch_evaluation_can_be_started_from_the_control_center(tmp_path: Path):
    settings = EvaluationSettings(
        cases_dir=ROOT / "eval/dev",
        manifest_path=MANIFEST,
        flagd_config=tmp_path / "flags.json",
        prometheus_url="http://prometheus.test",
        api_url="http://oncall.test",
        artifacts_dir=tmp_path / "runs",
        lock_file=tmp_path / "live.lock",
        fail_under=1.0,
    )
    jobs = EvaluationJobService(tmp_path / "jobs.sqlite3", settings)
    judge_settings = JudgeSettings(
        model="google/test-judge",
        api_key="test-key",
        rubric_path=ROOT / "eval/rubrics/investigation-v1.yaml",
        artifacts_dir=tmp_path,
    )

    def execute(_incidents, _cases, _runs, _judge, *, re_evaluate, emit):
        assert re_evaluate is True
        emit("judge", "saved-001: LLM rubric created")
        return BatchEvaluationSummary(1, 1, 1, 0, 0, 0, ("run-001",))

    batch = BatchEvaluationService(
        tmp_path / "jobs.sqlite3",
        settings,
        list,
        judge_settings=lambda: judge_settings,
        execute=execute,
    )
    app = create_app(
        manifest_path=MANIFEST,
        flag_path=_flags(tmp_path),
        getter=lambda _url: [],
        jobs=jobs,
        batch_jobs=batch,
    )
    with TestClient(app) as client:
        capabilities = client.get("/api/batch-evaluations/capabilities")
        started = client.post("/api/batch-evaluations", json={"re_evaluate": True})
        batch.join()
        detail = client.get(f"/api/batch-evaluations/{started.json()['job_id']}")

    assert capabilities.json()["judge_available"] is True
    assert started.status_code == 202
    assert detail.json()["state"] == "succeeded"
    assert detail.json()["summary"]["llm_created"] == 1
    assert detail.json()["events"][0]["phase"] == "judge"
