"""HTTP boundary between the private evaluator and the incident service."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from oncall_contracts.config import Topology
from oncall_contracts.faults import FaultAction
from oncall_contracts.state import Alert

from .scoring import SystemRun


class IncidentApiError(RuntimeError):
    """The incident API was unavailable or violated the evaluation contract."""


HttpTransport = Callable[[str, str, object | None, float], Any]


def _request_json(method: str, url: str, body: object | None, timeout: float) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read(1000).decode(errors="replace")
        raise IncidentApiError(f"incident API returned HTTP {exc.code}: {detail}") from exc
    except (OSError, TimeoutError, URLError, json.JSONDecodeError) as exc:
        raise IncidentApiError(f"incident API request failed: {exc}") from exc


class IncidentApiClient:
    """Submit an alert, approve its exact proposal, and read terminal evidence."""

    TERMINAL: ClassVar[frozenset[str]] = frozenset(
        {"RECOVERED", "REJECTED", "ESCALATED", "APPROVAL_MISMATCH"}
    )

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        request_timeout_seconds: float = 30.0,
        run_timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 1.0,
        auto_approve: bool = True,
        transport: HttpTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("incident API base_url must use http or https")
        for name, value in {
            "request_timeout_seconds": request_timeout_seconds,
            "run_timeout_seconds": run_timeout_seconds,
            "poll_interval_seconds": poll_interval_seconds,
        }.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self.base_url = base_url.rstrip("/")
        self.request_timeout_seconds = request_timeout_seconds
        self.run_timeout_seconds = run_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.auto_approve = auto_approve
        self.transport = transport or _request_json
        self.clock = clock
        self.sleep = sleep

    def preflight(self, config_id: Topology) -> None:
        """Fail before injection when the API cannot evaluate this topology."""
        payload = self._request("GET", "/healthz", None)
        if payload.get("status") != "ok":
            raise IncidentApiError("incident API health check did not return status 'ok'")
        capabilities = self._request("GET", "/api/evaluation-capabilities", None)
        configurations = capabilities.get("configurations")
        if not isinstance(configurations, list) or config_id not in configurations:
            raise IncidentApiError(
                f"incident API does not advertise evaluation support for {config_id!r}"
            )
        if capabilities.get("approval") != "exact-action-payload-hash":
            raise IncidentApiError("incident API does not advertise the exact-action approval gate")
        if capabilities.get("system_run_contract") != "v1":
            raise IncidentApiError("incident API does not advertise SystemRun contract v1")

    def run(
        self,
        alert: Alert,
        *,
        incident_id: str,
        config_id: Topology,
        tool_budget: int,
        expected_action: FaultAction | None,
    ) -> SystemRun:
        query = urlencode(
            {
                "incident_id": incident_id,
                "config_id": config_id,
                "tool_budget": tool_budget,
            }
        )
        payload = self._request("POST", f"/alerts?{query}", alert)
        deadline = self.clock() + self.run_timeout_seconds
        decided_hashes: set[str] = set()
        incident_path = f"/incidents/{quote(incident_id, safe='')}"

        while True:
            approval = payload.get("approval") or payload.get("interrupt")
            if approval is not None:
                if not isinstance(approval, Mapping):
                    raise IncidentApiError("approval must be an object")
                payload_hash = approval.get("payload_hash")
                if not isinstance(payload_hash, str) or not payload_hash:
                    raise IncidentApiError("approval has no payload_hash")
                if not self.auto_approve:
                    raise IncidentApiError("run requires approval but auto_approve is disabled")
                if payload_hash in decided_hashes:
                    now = self.clock()
                    if now >= deadline:
                        raise IncidentApiError(
                            f"incident {incident_id!r} did not clear its decided approval "
                            f"within {self.run_timeout_seconds}s"
                        )
                    self.sleep(min(self.poll_interval_seconds, deadline - now))
                    payload = self._request("GET", incident_path, None)
                    continue
                decided_hashes.add(payload_hash)
                decision = (
                    "approve"
                    if _same_action(approval.get("command"), expected_action)
                    else "reject"
                )
                payload = self._request(
                    "POST",
                    f"{incident_path}/approve",
                    {
                        "decision": decision,
                        "payload_hash": payload_hash if decision == "approve" else None,
                    },
                )
                continue

            status = str(payload.get("status") or "").upper()
            if status in self.TERMINAL:
                result = payload.get("system_run")
                if not isinstance(result, Mapping):
                    raise IncidentApiError(
                        f"terminal incident {incident_id!r} has no system_run evidence object"
                    )
                return cast(SystemRun, dict(result))

            now = self.clock()
            if now >= deadline:
                raise IncidentApiError(
                    f"incident {incident_id!r} did not terminate within {self.run_timeout_seconds}s"
                )
            self.sleep(min(self.poll_interval_seconds, deadline - now))
            payload = self._request("GET", incident_path, None)

    def _request(self, method: str, path: str, body: object | None) -> Mapping[str, Any]:
        payload = self.transport(
            method,
            f"{self.base_url}{path}",
            body,
            self.request_timeout_seconds,
        )
        if not isinstance(payload, Mapping):
            raise IncidentApiError("incident API response must be a JSON object")
        return payload


def _same_action(actual: object, expected: FaultAction | None) -> bool:
    """Match a command containing exactly the expected action and arguments."""
    if expected is None or not isinstance(actual, Mapping):
        return False
    if set(actual) != {"action", "args"}:
        return False
    return actual.get("action") == expected["action"] and actual.get("args") == expected["args"]
