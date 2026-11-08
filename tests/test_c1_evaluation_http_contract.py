from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from oncall_eval.http_client import IncidentApiClient
from oncall_service.main import create_app

ACTION = {
    "action": "set_flag",
    "args": {"flag": "paymentFailure", "variant": "off"},
}


class FakeShop:
    def __init__(self):
        self.writes = []


class ContractSolo:
    """Two-phase C1 stand-in: exercises HTTP approval without model or telemetry."""

    def __init__(self, **_kwargs):
        self.shop = FakeShop()

    def run(self, _alert=None, *, incident_id: str, resume=None):
        state = {
            "incident_id": incident_id,
            "status": "RUNNING",
            "plan": {"stage": "act", "steps": []},
            "hypotheses": [{"service": "payment", "confidence": 0.98}],
            "proposed_action": ACTION,
            "tool_calls": 4,
            "meter_rows": [
                {
                    "agent": "solo",
                    "model_id": "contract/c1",
                    "input_tokens": 10,
                    "output_tokens": 5,
                }
            ],
        }
        if resume is None:
            return (
                state
                | {
                    "__interrupt__": [
                        {
                            "command": ACTION,
                            "expected_effect": "payment recovers",
                            "payload_hash": "contract-hash",
                        }
                    ]
                },
                {},
            )
        assert resume == {"decision": "approve", "payload_hash": "contract-hash"}
        self.shop.writes.append({"tool": "set_flag", "args": ACTION["args"]})
        return (
            state
            | {
                "status": "RECOVERED",
                "verification": {"recovered": True},
            },
            {},
        )


def test_c1_evaluator_and_incident_service_share_one_http_contract(monkeypatch):
    monkeypatch.setattr("oncall_service.main.Solo", ContractSolo)
    monkeypatch.setattr("oncall_service.main.LiveShop", FakeShop)
    app = create_app(demo_delay=0)

    with TestClient(app) as service:

        def transport(method: str, url: str, body, _timeout: float):
            parsed = urlsplit(url)
            response = service.request(
                method,
                parsed.path + (f"?{parsed.query}" if parsed.query else ""),
                json=body,
            )
            response.raise_for_status()
            return response.json()

        evaluator = IncidentApiClient(
            "http://contract.test",
            transport=transport,
            poll_interval_seconds=0.001,
            run_timeout_seconds=2,
        )
        evaluator.preflight("c1_single")
        result = evaluator.run(
            {
                "status": "firing",
                "labels": {"alertname": "CheckoutErrorRateHigh", "service": "checkout"},
                "annotations": {"summary": "Checkout failures"},
                "startsAt": "2026-11-02T00:00:00Z",
            },
            incident_id="eval-contract-c1",
            config_id="c1_single",
            tool_budget=10,
            expected_action=ACTION,  # type: ignore[arg-type]
        )

    assert result.get("error") is None, result
    assert result["root_cause_services"] == ["payment"]
    assert result["proposed_action"] == ACTION
    assert result["executed_actions"] == [ACTION]
    assert result["verified_recovery"] is True
    assert result["model_ids"] == {"solo": "contract/c1"}
