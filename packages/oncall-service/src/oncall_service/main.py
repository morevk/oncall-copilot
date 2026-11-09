"""Incident API, durable event history, SSE stream, and operations console."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from oncall_contracts.config import Topology
from oncall_contracts.state import Alert
from oncall_graph.astronomy_shop import LiveShop
from oncall_graph.build import open_tools
from oncall_graph.config import DEFAULT_TOOL_BUDGET, LIVE_TOOL_BUDGET
from oncall_graph.nodes.remediate import command_payload, payload_hash
from oncall_graph.stubs import Shop
from oncall_graph.topologies.c1_single import Solo, interrupt_value
from oncall_graph.topologies.c2_team import Team
from oncall_memory import LessonStore
from pydantic import BaseModel

from .alerts import AlertPayloadError, firing_alerts, is_notification_group
from .database import IncidentDatabase

STATIC = Path(__file__).with_name("static")
TERMINAL = frozenset({"RECOVERED", "REJECTED", "ESCALATED", "APPROVAL_MISMATCH"})
RUNNABLE: frozenset[Topology] = frozenset({"c1_single", "c2_team"})


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


class ApprovalRequest(BaseModel):
    decision: str
    payload_hash: str | None = None


@dataclass
class Incident:
    incident_id: str
    mode: str
    alert: Alert
    config_id: Topology
    tool_budget: int
    status: str = "QUEUED"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    stage: str = "scope"
    state: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] | None = None
    system_run: dict[str, Any] | None = None
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    runner: Solo | Team | None = None
    running: bool = False

    def snapshot(self) -> dict[str, Any]:
        value = {
            "incident_id": self.incident_id,
            "mode": self.mode,
            "status": self.status,
            "terminal": self.status in TERMINAL,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "config_id": self.config_id,
            "tool_budget": self.tool_budget,
            "stage": self.stage,
            "alert": self.alert,
            "state": self.state,
            "approval": self.approval,
            "system_run": self.system_run,
            "error": self.error,
            "event_count": len(self.events),
            "events": self.events,
        }
        return _json_safe(value)


class IncidentStore:
    def __init__(self, demo_delay: float = 0.65, database_path: Path | None = None):
        self.incidents: dict[str, Incident] = {}
        self.demo_delay = demo_delay
        self.feed_events: list[dict[str, Any]] = []
        self.feed_condition = asyncio.Condition()
        # fingerprint (or alertname + startsAt) -> incident id. Grafana resends
        # the same firing group on every evaluation; a terminal incident does
        # not block the next occurrence.
        self.alert_keys: dict[str, str] = {}
        self.database = IncidentDatabase(database_path) if database_path is not None else None
        if self.database is not None:
            self._restore()

    def _restore(self) -> None:
        assert self.database is not None
        snapshots, self.feed_events, self.alert_keys = self.database.load()
        for snapshot in snapshots:
            incident = Incident(
                incident_id=snapshot["incident_id"],
                mode=snapshot["mode"],
                alert=snapshot["alert"],
                config_id=snapshot["config_id"],
                tool_budget=snapshot["tool_budget"],
                status=snapshot["status"],
                created_at=snapshot["created_at"],
                updated_at=snapshot["updated_at"],
                stage=snapshot["stage"],
                state=snapshot["state"],
                approval=snapshot["approval"],
                system_run=snapshot["system_run"],
                error=snapshot["error"],
                events=snapshot["events"],
            )
            self.incidents[incident.incident_id] = incident
        for incident in self.incidents.values():
            if incident.status not in TERMINAL:
                self._interrupt(incident)

    def _interrupt(self, incident: Incident) -> None:
        incident.status = "ESCALATED"
        incident.approval = None
        incident.error = "incident interrupted by service restart; agent execution was not resumed"
        self._append_event(incident, "incident.interrupted", incident.error)

    def reserve_alert(self, key: str, incident_id: str) -> Incident | None:
        """Claim this firing alert, or return the investigation already open for it."""
        current = self.alert_keys.get(key)
        if current:
            incident = self.incidents.get(current)
            if incident is not None and incident.status not in TERMINAL:
                return incident
        self.alert_keys[key] = incident_id
        if self.database is not None:
            self.database.reserve_alert(key, incident_id)
        return None

    def get(self, incident_id: str) -> Incident:
        incident = self.incidents.get(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        return incident

    async def create(
        self,
        *,
        alert: Alert,
        incident_id: str,
        mode: str,
        config_id: Topology,
        tool_budget: int,
    ) -> Incident:
        if incident_id in self.incidents:
            raise HTTPException(status_code=409, detail="incident id already exists")
        incident = Incident(
            incident_id=incident_id,
            mode=mode,
            alert=alert,
            config_id=config_id,
            tool_budget=tool_budget,
            state={
                "plan": {"stage": "scope", "steps": []},
                "hypotheses": [],
                "evidence": [],
                "transcript": [],
                "tool_calls": 0,
            },
        )
        self.incidents[incident_id] = incident
        await self.emit(incident, "incident.created", "Alert accepted; investigation queued")
        return incident

    async def emit(self, incident: Incident, kind: str, message: str) -> None:
        self._append_event(incident, kind, message)
        async with incident.condition:
            incident.condition.notify_all()
        async with self.feed_condition:
            self.feed_condition.notify_all()

    def _append_event(self, incident: Incident, kind: str, message: str) -> None:
        incident.updated_at = _now()
        event = {
            "id": len(incident.events) + 1,
            "type": kind,
            "time": incident.updated_at,
            "message": message,
        }
        incident.events.append(event)
        feed_event = {
            "id": len(self.feed_events) + 1,
            "type": kind,
            "time": incident.updated_at,
            "incident": incident.snapshot(),
        }
        self.feed_events.append(feed_event)
        if self.database is not None:
            self.database.save_event(incident.snapshot(), event, feed_event)


def _validate_alert(value: Any) -> Alert:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="alert must be an object")
    required = {"status", "labels", "annotations", "startsAt"}
    if missing := required - set(value):
        raise HTTPException(status_code=422, detail=f"alert missing fields {sorted(missing)}")
    if not isinstance(value.get("labels"), dict) or not isinstance(value.get("annotations"), dict):
        raise HTTPException(status_code=422, detail="alert labels and annotations must be objects")
    return value  # type: ignore[return-value]


def _public_graph_state(raw: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "scope",
        "severity",
        "hypotheses",
        "evidence",
        "plan",
        "verification",
        "transcript",
        "proposed_action",
        "tool_calls",
        "why",
    )
    return _json_safe({field: raw.get(field) for field in fields if raw.get(field) is not None})


def _system_run(incident: Incident, raw: dict[str, Any]) -> dict[str, Any]:
    hypotheses = sorted(
        (raw.get("hypotheses") or []),
        key=lambda item: float(item.get("confidence") or 0) if isinstance(item, dict) else 0,
        reverse=True,
    )
    roots = [
        item.get("service") for item in hypotheses if isinstance(item, dict) and item.get("service")
    ]
    proposal = raw.get("proposed_action")
    proposed_action = None
    if isinstance(proposal, dict) and proposal.get("action"):
        proposed_action = {"action": proposal["action"], "args": dict(proposal.get("args") or {})}
    writes = incident.runner.shop.writes if incident.runner is not None else []
    executed = [
        {"action": item.get("tool"), "args": dict(item.get("args") or {})}
        for item in writes
        if isinstance(item, dict) and item.get("tool")
    ]
    rows = raw.get("meter_rows") or []
    model_ids = {
        str(row.get("agent") or "agent"): str(row.get("model_id"))
        for row in rows
        if isinstance(row, dict) and row.get("model_id")
    }
    return {
        "root_cause_services": roots,
        "proposed_action": proposed_action,
        "executed_actions": executed,
        "tool_calls": int(raw.get("tool_calls") or 0),
        "tokens": sum(
            int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
            for row in rows
            if isinstance(row, dict)
        ),
        "usd": 0.0,
        "verified_recovery": bool((raw.get("verification") or {}).get("recovered")),
        "rolled_back": any(
            isinstance(item, dict) and item.get("tool") == "rollback" for item in writes
        ),
        "model_ids": model_ids,
        "playbook_hash": _playbook_hash(incident),
        "error": incident.error,
    }


async def _start_live(
    store: IncidentStore,
    alert: Alert,
    *,
    incident_id: str,
    config_id: Topology,
    tool_budget: int,
) -> Incident:
    incident = await store.create(
        alert=alert,
        incident_id=incident_id,
        mode="hybrid-live",
        config_id=config_id,
        tool_budget=tool_budget,
    )
    asyncio.create_task(_run_real(store, incident))
    return incident


async def _run_real(store: IncidentStore, incident: Incident, resume: dict | None = None) -> None:
    incident.running = True
    incident.status = "INVESTIGATING" if resume is None else "EXECUTING"
    await store.emit(
        incident,
        "agent.started" if resume is None else "approval.accepted",
        "Agent investigation started" if resume is None else "Approved command resumed",
    )
    try:
        if resume is None:
            incident.runner = _open_runner(incident, store.tools)
            raw, _measured = await asyncio.to_thread(
                incident.runner.run,
                incident.alert,
                incident_id=incident.incident_id,
            )
        else:
            if incident.runner is None:
                raise RuntimeError("incident graph is not available for resume")
            raw, _measured = await asyncio.to_thread(
                incident.runner.run,
                incident_id=incident.incident_id,
                resume=resume,
            )
        incident.state = _public_graph_state(raw)
        incident.stage = str((raw.get("plan") or {}).get("stage") or raw.get("stage") or "scope")
        approval = interrupt_value(raw)
        if isinstance(approval, dict):
            incident.approval = _json_safe(approval)
            incident.status = "AWAITING_APPROVAL"
            await store.emit(incident, "approval.requested", "One structured action needs review")
        else:
            incident.approval = None
            incident.status = str(raw.get("status") or "ESCALATED")
            if incident.status not in TERMINAL:
                incident.status = "ESCALATED"
            incident.system_run = _system_run(incident, raw)
            await store.emit(incident, "incident.terminal", f"Incident finished: {incident.status}")
    except Exception as exc:  # noqa: BLE001 - background failures must become visible incident state
        incident.error = f"{type(exc).__name__}: {exc}"
        incident.status = "ESCALATED"
        incident.system_run = _system_run(incident, {})
        await store.emit(incident, "incident.failed", incident.error)
    finally:
        incident.running = False


async def _run_demo(store: IncidentStore, incident: Incident) -> None:
    delay = store.demo_delay

    async def step(kind: str, message: str) -> None:
        if delay:
            await asyncio.sleep(delay)
        await store.emit(incident, kind, message)

    incident.status = "INVESTIGATING"
    incident.stage = "scope"
    incident.state["transcript"].append({"kind": "input_guard", "text": "Alert treated as data"})
    await step("agent.started", "Solo investigator started with a 10-call budget")

    incident.stage = "test"
    incident.state["plan"] = {
        "stage": "test",
        "steps": [
            {"tool": "promql", "args": {"query": "checkout error ratio"}},
            {"tool": "traces", "args": {"service": "payment"}},
        ],
    }
    await step("plan.updated", "Testing checkout symptoms against payment telemetry")

    incident.state["tool_calls"] = 1
    incident.state["evidence"].append(
        {"kind": "promql", "ref": "checkout_422_ratio", "source": "Prometheus"}
    )
    incident.state["transcript"].append({"kind": "evidence", "text": "Checkout 422 ratio is 0.91"})
    await step("evidence.added", "Prometheus confirms sustained checkout failures")

    incident.state["tool_calls"] = 2
    incident.state["evidence"].append(
        {"kind": "trace", "ref": "trace-demo-84f2", "source": "Jaeger"}
    )
    incident.state["hypotheses"] = [
        {
            "service": "payment",
            "statement": "paymentFailure flag rejects charge requests",
            "evidence_refs": ["checkout_422_ratio", "trace-demo-84f2"],
            "confidence": 0.91,
        }
    ]
    await step("hypothesis.updated", "Trace evidence localises the failure to payment")

    action = {
        "action": "set_flag",
        "args": {"flag": "paymentFailure", "variant": "off"},
        "expected_effect": "Payment transactions resume and checkout 422s fall",
        "rollback": "rollback_flag paymentFailure to its previous variant",
    }
    command = command_payload(action)
    incident.state["proposed_action"] = action
    incident.state["plan"] = {"stage": "act", "steps": []}
    incident.stage = "act"
    incident.status = "AWAITING_APPROVAL"
    incident.approval = {
        "command": {"action": command["action"], "args": command["args"]},
        "expected_effect": command["expected_effect"],
        "payload_hash": payload_hash(command),
    }
    await step("approval.requested", "Disable paymentFailure requires exact human approval")


async def _resume_demo(store: IncidentStore, incident: Incident, decision: str) -> None:
    if decision == "reject":
        incident.status = "REJECTED"
        incident.approval = None
        incident.state["transcript"].append({"kind": "approval", "text": "Action rejected"})
        await store.emit(incident, "approval.rejected", "Operator rejected the proposed action")
        return
    incident.status = "APPROVED"
    incident.approval = None
    incident.state["transcript"].append({"kind": "approval", "text": "Exact action approved"})
    await store.emit(incident, "approval.accepted", "Exact payload hash approved")
    if store.demo_delay:
        await asyncio.sleep(store.demo_delay)
    incident.status = "VERIFYING"
    incident.stage = "verify"
    incident.state["plan"] = {"stage": "verify", "steps": []}
    await store.emit(incident, "action.executed", "paymentFailure set to off")
    if store.demo_delay:
        await asyncio.sleep(store.demo_delay)
    incident.status = "RECOVERED"
    incident.stage = "communicate"
    incident.state["verification"] = {
        "recovered": True,
        "signal": "checkout_422_ratio",
        "evidence_refs": ["checkout_422_ratio"],
    }
    incident.state["transcript"].append(
        {"kind": "verify", "text": "Checkout error ratio returned below 5%"}
    )
    incident.system_run = {
        "root_cause_services": ["payment"],
        "proposed_action": {
            "action": "set_flag",
            "args": {"flag": "paymentFailure", "variant": "off"},
        },
        "executed_actions": [
            {
                "action": "set_flag",
                "args": {"flag": "paymentFailure", "variant": "off"},
            }
        ],
        "tool_calls": 2,
        "tokens": 0,
        "usd": 0.0,
        "verified_recovery": True,
        "rolled_back": False,
        "model_ids": {"solo": "fixture/demo"},
        "playbook_hash": None,
        "error": None,
    }
    await store.emit(incident, "verification.recovered", "Recovery verified below alert threshold")


def _require_runnable(config_id: Topology) -> None:
    if config_id not in RUNNABLE:
        raise HTTPException(
            status_code=422,
            detail="only c1_single and c2_team are implemented",
        )


def _resolve_tool_budget(config_id: Topology, tool_budget: int | None) -> int:
    """An omitted budget follows the live default for that topology."""
    if tool_budget is not None:
        return tool_budget
    if config_id == "c2_team":
        return LIVE_TOOL_BUDGET
    return DEFAULT_TOOL_BUDGET


def _playbook_hash(incident: Incident) -> str | None:
    """The playbook the team run actually filtered. Solo does not read one."""
    store = getattr(incident.runner, "store", None)
    digest = getattr(store, "playbook_hash", None)
    return digest if isinstance(digest, str) and digest else None


def _open_runner(incident: Incident, tools=None) -> Solo | Team:
    """The same instance must resume, because its checkpointer holds the pause.

    A registry is what the model reads. apply and verify still use the
    scripted shop inside the runner; those two calls are not this seam.
    """
    if tools is not None:
        # Reads come from the registry. The shop is only what apply and
        # verify still call, and what the run record's write list is read from.
        shop = Shop()
        if incident.config_id == "c1_single":
            return Solo(shop=shop, tools=tools, tool_budget=incident.tool_budget)
        if incident.config_id == "c2_team":
            return Team(
                shop=shop,
                tools=tools,
                store=LessonStore.from_postmortems(),
                tool_budget=incident.tool_budget,
            )
        raise RuntimeError(f"unsupported topology {incident.config_id!r}")
    shop = LiveShop()
    if incident.config_id == "c1_single":
        return Solo(shop=shop, tool_budget=incident.tool_budget)
    if incident.config_id == "c2_team":
        # Objective 3. c1_single does not filter lessons; the team runbook step does.
        return Team(
            shop=shop,
            store=LessonStore.from_postmortems(),
            tool_budget=incident.tool_budget,
        )
    raise RuntimeError(f"unsupported topology {incident.config_id!r}")


async def _accept_notification(
    store: IncidentStore,
    body: dict[str, Any],
    *,
    config_id: Topology,
    tool_budget: int,
) -> dict[str, Any]:
    """Start one investigation per firing alert. A repeat of an open one is a no-op."""
    try:
        batch = firing_alerts(body)
    except AlertPayloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    snapshots = []
    for alert, key in batch:
        incident_id = f"alert-{uuid.uuid4().hex[:12]}"
        existing = store.reserve_alert(key, incident_id)
        if existing is not None:
            snapshots.append(existing.snapshot())
            continue
        incident = await _start_live(
            store,
            alert,
            incident_id=incident_id,
            config_id=config_id,
            tool_budget=tool_budget,
        )
        snapshots.append(incident.snapshot())
    return {"incidents": snapshots}


def create_app(
    *,
    demo_delay: float = 0.65,
    cors_origins: Sequence[str] | None = None,
    database_path: Path | None = None,
    tools: Any = None,
    connect_tools: bool = False,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        opened = None
        if connect_tools and store.tools is None:
            opened = open_tools()
            store.tools = opened
        try:
            yield
        finally:
            if opened is not None:
                opened.close()
                store.tools = None

    app = FastAPI(title="On-Call Copilot", version="0.1.0", lifespan=lifespan)
    if cors_origins is None:
        cors_origins = tuple(
            origin.strip()
            for origin in os.environ.get("ONCALL_CORS_ORIGINS", "").split(",")
            if origin.strip()
        )
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Last-Event-ID"],
        )
    configured_database = database_path
    if configured_database is None and (value := os.environ.get("ONCALL_INCIDENT_DATABASE")):
        configured_database = Path(value)
    store = IncidentStore(demo_delay=demo_delay, database_path=configured_database)
    store.tools = tools
    app.state.incidents = store
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/evaluation-capabilities")
    async def evaluation_capabilities() -> dict[str, Any]:
        """Public contract checked by the evaluator before it mutates the lab."""
        return {
            "configurations": ["c1_single", "c2_team"],
            "approval": "exact-action-payload-hash",
            "system_run_contract": "v1",
        }

    @app.get("/api/incidents")
    async def list_incidents() -> list[dict[str, Any]]:
        return [
            incident.snapshot()
            for incident in sorted(
                store.incidents.values(), key=lambda item: item.created_at, reverse=True
            )
        ]

    @app.get("/api/incidents/stream")
    async def stream_incidents(
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            cursor = max(0, int(last_event_id or 0))
        except ValueError:
            cursor = 0
        cursor = min(cursor, len(store.feed_events))

        async def events():
            nonlocal cursor
            while True:
                while cursor < len(store.feed_events):
                    event = store.feed_events[cursor]
                    cursor += 1
                    yield (
                        f"id: {event['id']}\n"
                        "event: incident\n"
                        f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                    )
                if await request.is_disconnected():
                    return
                try:
                    async with store.feed_condition:
                        await asyncio.wait_for(store.feed_condition.wait(), timeout=15)
                except TimeoutError:
                    yield ": heartbeat\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/demo", status_code=202)
    async def start_demo() -> dict[str, Any]:
        incident_id = f"demo-{uuid.uuid4().hex[:8]}"
        alert: Alert = {
            "status": "firing",
            "labels": {
                "alertname": "CheckoutErrorRateHigh",
                "service": "checkout",
                "severity": "critical",
            },
            "annotations": {"summary": "Checkout failures are elevated above 5%"},
            "startsAt": _now(),
        }
        incident = await store.create(
            alert=alert,
            incident_id=incident_id,
            mode="demo",
            config_id="c1_single",
            tool_budget=10,
        )
        asyncio.create_task(_run_demo(store, incident))
        return incident.snapshot()

    @app.post("/alerts", status_code=202)
    async def post_alert(
        alert_body: Annotated[dict[str, Any], Body()],
        incident_id: Annotated[str | None, Query()] = None,
        config_id: Annotated[Topology, Query()] = "c1_single",
        tool_budget: Annotated[int | None, Query(ge=1, le=100)] = None,
    ) -> dict[str, Any]:
        _require_runnable(config_id)
        resolved_budget = _resolve_tool_budget(config_id, tool_budget)
        if is_notification_group(alert_body):
            return await _accept_notification(
                store,
                alert_body,
                config_id=config_id,
                tool_budget=resolved_budget,
            )
        incident = await _start_live(
            store,
            _validate_alert(alert_body),
            incident_id=incident_id or f"incident-{uuid.uuid4().hex[:12]}",
            config_id=config_id,
            tool_budget=resolved_budget,
        )
        return incident.snapshot()

    @app.get("/incidents/{incident_id}")
    async def get_incident(incident_id: str) -> dict[str, Any]:
        return store.get(incident_id).snapshot()

    @app.get("/incidents/{incident_id}/stream")
    async def stream_incident(
        request: Request,
        incident_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        incident = store.get(incident_id)
        try:
            cursor = max(0, int(last_event_id or 0))
        except ValueError:
            cursor = 0

        async def events():
            nonlocal cursor
            while True:
                while cursor < len(incident.events):
                    event = incident.events[cursor]
                    cursor += 1
                    payload = {**event, "incident": incident.snapshot()}
                    yield (
                        f"id: {event['id']}\n"
                        "event: update\n"
                        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                    )
                if await request.is_disconnected():
                    return
                try:
                    async with incident.condition:
                        await asyncio.wait_for(incident.condition.wait(), timeout=15)
                except TimeoutError:
                    yield ": heartbeat\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/incidents/{incident_id}/approve")
    async def decide(incident_id: str, request_body: ApprovalRequest) -> dict[str, Any]:
        incident = store.get(incident_id)
        decision = request_body.decision.strip().lower()
        if decision not in {"approve", "approved", "reject", "rejected"}:
            raise HTTPException(status_code=422, detail="decision must be approve or reject")
        if incident.status != "AWAITING_APPROVAL" or incident.approval is None:
            raise HTTPException(status_code=409, detail="incident is not awaiting approval")
        expected_hash = str(incident.approval.get("payload_hash") or "")
        if decision in {"approve", "approved"} and request_body.payload_hash != expected_hash:
            raise HTTPException(status_code=409, detail="approval payload hash is stale")
        if incident.running:
            raise HTTPException(status_code=409, detail="incident is already running")
        normalised = "approve" if decision in {"approve", "approved"} else "reject"
        if incident.mode == "demo":
            asyncio.create_task(_resume_demo(store, incident, normalised))
        else:
            resume = (
                {"decision": "approve", "payload_hash": expected_hash}
                if normalised == "approve"
                else {"decision": "reject"}
            )
            asyncio.create_task(_run_real(store, incident, resume=resume))
        return incident.snapshot()

    return app


app = create_app(connect_tools=True)


def run() -> None:
    import uvicorn

    uvicorn.run(
        "oncall_service.main:app",
        host=os.environ.get("ONCALL_SERVICE_HOST", "127.0.0.1"),
        port=int(os.environ.get("ONCALL_SERVICE_PORT", "8000")),
        reload=False,
    )
