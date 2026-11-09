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

import os
from pathlib import Path

from oncall_contracts.flagd import current_variant, read_document
from oncall_tools.servers.ops_actions import (
    DEFAULT_FLAG_PATH,
    FLAG_PATH_ENV,
    rollback_flag,
    set_flag,
)
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
    "TelemetryNotConfigured",
    "TelemetryUnavailable",
]


class LiveShop(Shop):
    """Live reads, and writes that go through the two ops-actions tools.

    ``set_flag`` applies a runbook fix. ``rollback_flag`` restores the variant
    the forward write reported. Anything else is refused. ``armed`` reads the
    flagd document, so a write that never lands cannot look like recovery.
    """

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

    def armed(self, flag: str = "paymentFailure") -> bool:
        path = Path(os.environ.get(FLAG_PATH_ENV) or DEFAULT_FLAG_PATH)
        return current_variant(read_document(path), flag) != self.DISARMED

    def apply(self, tool, args):
        """Write the flagd document. The in-memory ``flags`` dict is not the shop."""
        clean = dict(args or {})
        if tool == "set_flag":
            reported = set_flag(str(clean.get("flag") or ""), str(clean.get("variant") or ""))
        elif tool == "rollback_flag":
            reported = rollback_flag(str(clean.get("flag") or ""), str(clean.get("variant") or ""))
        else:
            raise ValueError(f"unknown tool: {tool!r}")
        self.writes.append({"tool": tool, "args": clean})
        return {
            "ok": True,
            "tool": tool,
            "args": clean,
            "flag": reported["flag"],
            "variant": reported["variant"],
            "previous": reported["previous"],
            "changed": reported["changed"],
        }
