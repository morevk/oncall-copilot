"""Which consoles the Control Center shows, and how the lab decides each is up.

Browser links are the host URLs from `make urls`. Probe URLs are where this
process can reach the same service. In Compose those are Docker DNS names;
on the host they are localhost. A probe URL is never returned to the page.
"""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

Kind = Literal["http", "mcp", "self"]


@dataclass(frozen=True)
class ServiceCheck:
    service_id: str
    name: str
    group: str
    href: str
    probe: str | None
    kind: Kind


def service_checks(env: Mapping[str, str]) -> tuple[ServiceCheck, ...]:
    """Operator consoles. `env` overrides only the in-network probe URL."""

    def probe(key: str, fallback: str) -> str:
        value = env.get(key, "").strip()
        return value or fallback

    return (
        ServiceCheck(
            "web-store",
            "Web store",
            "Shop consoles",
            "http://localhost:8080/",
            probe("ONCALL_PROBE_SHOP", "http://127.0.0.1:8080/"),
            "http",
        ),
        ServiceCheck(
            "grafana",
            "Grafana",
            "Shop consoles",
            "http://localhost:8080/grafana/",
            probe("ONCALL_PROBE_GRAFANA", "http://127.0.0.1:8080/grafana/api/health"),
            "http",
        ),
        ServiceCheck(
            "jaeger",
            "Jaeger",
            "Shop consoles",
            "http://localhost:8080/jaeger/ui/",
            probe("ONCALL_PROBE_JAEGER", "http://127.0.0.1:8080/jaeger/ui/"),
            "http",
        ),
        ServiceCheck(
            "feature-flags",
            "Feature flags",
            "Shop consoles",
            "http://localhost:8080/feature",
            probe("ONCALL_PROBE_FLAGS", "http://127.0.0.1:8080/feature"),
            "http",
        ),
        ServiceCheck(
            "prometheus",
            "Prometheus",
            "Shop consoles",
            "http://localhost:9090/",
            probe("ONCALL_PROBE_PROMETHEUS", "http://127.0.0.1:9090/-/ready"),
            "http",
        ),
        ServiceCheck(
            "incidents",
            "Incidents",
            "On-call copilot",
            "http://localhost:8000/",
            probe("ONCALL_PROBE_INCIDENTS", "http://127.0.0.1:8000/healthz"),
            "http",
        ),
        ServiceCheck(
            "lab",
            "Lab",
            "On-call copilot",
            "http://localhost:8020/",
            None,
            "self",
        ),
        ServiceCheck(
            "evaluations",
            "Eval reports",
            "On-call copilot",
            "http://localhost:8030/",
            probe("ONCALL_PROBE_EVAL", "http://127.0.0.1:8030/healthz"),
            "http",
        ),
        ServiceCheck(
            "mcp-telemetry",
            "mcp-telemetry",
            "On-call copilot",
            "http://localhost:8101/mcp",
            probe("ONCALL_PROBE_MCP_TELEMETRY", "http://127.0.0.1:8101/mcp"),
            "mcp",
        ),
        ServiceCheck(
            "mcp-runbooks",
            "mcp-runbooks",
            "On-call copilot",
            "http://localhost:8102/mcp",
            probe("ONCALL_PROBE_MCP_RUNBOOKS", "http://127.0.0.1:8102/mcp"),
            "mcp",
        ),
        ServiceCheck(
            "mcp-ops-actions",
            "mcp-ops-actions",
            "On-call copilot",
            "http://localhost:8104/mcp",
            probe("ONCALL_PROBE_MCP_OPS", "http://127.0.0.1:8104/mcp"),
            "mcp",
        ),
    )


def probe_url(url: str, timeout: float = 2.0) -> int | None:
    """HTTP status, or None when nothing answered. 4xx and 5xx are statuses."""
    request = Request(url, method="GET", headers={"Accept": "*/*"})
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read(256)
            return int(response.status)
    except HTTPError as exc:
        exc.read(256)
        return int(exc.code)
    except (OSError, TimeoutError, URLError):
        return None


def _detail(kind: Kind, code: int | None) -> tuple[str, str]:
    if kind == "self":
        return "up", "This process"
    if code is None:
        return "down", "Unreachable"
    if kind == "mcp":
        return "up", f"HTTP {code}. A browser GET is refused; the server is listening."
    if code < 400:
        return "up", f"HTTP {code}"
    return "down", f"HTTP {code}"


def _one(check: ServiceCheck, prober) -> dict[str, str]:
    code = None if check.kind == "self" or check.probe is None else prober(check.probe)
    status, detail = _detail(check.kind, code)
    return {
        "id": check.service_id,
        "name": check.name,
        "href": check.href,
        "status": status,
        "detail": detail,
    }


def service_report(checks: tuple[ServiceCheck, ...], prober=probe_url) -> dict[str, list]:
    """Probe together. One hung console must not wait out the others in series."""
    if not checks:
        return {"groups": []}
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        rows = list(pool.map(lambda check: _one(check, prober), checks))
    groups: list[dict[str, object]] = []
    for check, row in zip(checks, rows, strict=True):
        if not groups or groups[-1]["name"] != check.group:
            groups.append({"name": check.group, "services": []})
        groups[-1]["services"].append(row)
    return {"groups": groups}
