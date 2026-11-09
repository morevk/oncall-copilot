"""`LiveShop`: the scripted shop with its three reads pointed at real telemetry.

THE CLIENT MOVED. `AstronomyShop` now lives in
`oncall_tools.servers.telemetry.client`, because `mcp-telemetry` serves it and
`make guards` forbids `oncall_tools` from importing `oncall_graph`. It is
re-exported here so `from oncall_graph.astronomy_shop import AstronomyShop`
still resolves, and so there is exactly one definition of what a PromQL query
is -- the thing two `set_flag` definitions cost us in #21.

TWO BEHAVIOURS CHANGED WITH IT, and they change what this class does:

* An unreachable Prometheus, Jaeger or OpenSearch now RAISES
  `TelemetryUnavailable` instead of returning `{"error": ...}`. A caller that
  treated the old dict as telemetry would have read a refused connection as an
  observation. `Solo` has no handler for it, so a dead backend now stops the
  run instead of quietly producing an unsupported diagnosis.
* There is no default host. `LiveShop()` with none of PROMETHEUS_URL,
  JAEGER_URL or OPENSEARCH_URL set raises `TelemetryNotConfigured` rather than
  querying `10.241.2.69`, a Kubernetes NodePort address from somebody's
  cluster. Compose sets all three on `oncall-service`, so the container path is
  unchanged; a host-side `solo_demo` run needs them in the environment, or
  `LiveShop(host="localhost")` against `make -C deploy up`.

THE TRACE LINES STAY HERE. They are `print`s, and in a stdio MCP server stdout
is the JSON-RPC channel, so the client logs instead. `LiveShop` keeps printing,
because `solo_demo` is a terminal demo where that is the whole point.
"""

from __future__ import annotations

import math
from typing import Any

from oncall_tools.registry import ToolRegistry
from oncall_tools.servers.telemetry.client import (
    AstronomyShop,
    TelemetryNotConfigured,
    TelemetryUnavailable,
)

from .meter import say
from .stubs import Shop

__all__ = [
    "AstronomyShop",
    "LiveShop",
    "RegistryShop",
    "TelemetryNotConfigured",
    "TelemetryUnavailable",
]


# The alert and eval harness use this same ratio. Keeping verification on the
# alert signal makes a successful write insufficient by itself: traffic must
# show that checkout recovered.
CHECKOUT_ERROR_RATIO = """
sum(rate(http_server_request_duration_seconds_count{
service_name="frontend",http_response_status_code="422"}[5m]))
/ (sum(rate(http_server_request_duration_seconds_count{
service_name="frontend",http_response_status_code="422"}[5m]))
+ sum(rate(demo_payment_transactions_total[5m])))
""".strip()


class LiveShop(Shop):
    """Scripted writes and reads, with the three telemetry tools pointed at the lab."""

    def __init__(self, host: str | None = None, minutes: int = 5, fault: str = "paymentFailure"):
        super().__init__(fault=fault)
        self.telemetry = AstronomyShop(host=host, minutes=minutes)

    def promql(self, query: str) -> dict:
        say(f"      astronomy shop promql query={query!r}")
        self.reads.append(("promql", {"query": query}))
        return self.telemetry.promql(query)

    def log_search(self, service: str, query: str = "") -> dict:
        say(f"      astronomy shop log_search service={service!r}")
        self.reads.append(("log_search", {"service": service, "query": query}))
        return self.telemetry.log_search(service, query)

    def traces(self, service: str) -> dict:
        say(f"      astronomy shop traces service={service!r}")
        self.reads.append(("traces", {"service": service}))
        return self.telemetry.traces(service)


class RegistryShop(Shop):
    """Execute approved writes and recovery reads through one MCP registry.

    Diagnosis agents already receive narrowed registry tools. This adapter is
    deliberately used only by code-owned remediation nodes after approval: it
    records a write only after the MCP server succeeds, and it refuses to turn
    missing or non-numeric Prometheus data into a healthy zero.
    """

    def __init__(self, registry: ToolRegistry):
        # Keep Shop's audit fields and interface; its scripted flag state is
        # never consulted by this adapter.
        super().__init__()
        self.registry = registry

    def apply(self, tool: str, args: dict[str, Any]) -> Any:
        spec = self.registry[tool]
        if not spec.write:
            raise ValueError(f"refusing to execute read tool {tool!r} as an action")
        clean = dict(args or {})
        result = spec.invoke(clean)
        self.writes.append({"tool": tool, "args": clean})
        return result

    def metrics(self) -> dict[str, Any]:
        spec = self.registry["promql"]
        if spec.write:
            raise ValueError("promql must be a read tool")
        result = spec.invoke({"query": CHECKOUT_ERROR_RATIO})
        if not isinstance(result, dict):
            raise TypeError("promql recovery check returned a non-object result")
        series = result.get("series")
        if not isinstance(series, list) or not series:
            raise RuntimeError("promql recovery check returned no series")
        rates: list[float] = []
        for row in series:
            value = row.get("value") if isinstance(row, dict) else None
            if not isinstance(value, (list, tuple)) or len(value) < 2:
                raise RuntimeError("promql recovery series has no value")
            try:
                rate = float(value[1])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("promql recovery value is not numeric") from exc
            if not math.isfinite(rate):
                raise RuntimeError("promql recovery value is not finite")
            rates.append(rate)
        return {"error_rate": max(rates), "service": "checkout", "source": "promql"}
