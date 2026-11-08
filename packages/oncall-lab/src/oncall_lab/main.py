"""Operator lab for the Astronomy Shop.

This process can arm a fault in the flag file, post one symptom to the
incident API, and start one evaluation at a time. It does not run the agent
and it does not share the incident store. The responder console stays on the
incident service.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from oncall_contracts.flagd import (
    FlagEditError,
    FlagFileError,
    current_variant,
    read_document,
    set_variant,
    write_document,
)
from oncall_eval.batch import incident_evaluation_status
from oncall_eval.execution import resolve_cases
from oncall_eval.retrospective import RetrospectiveEvaluationError, evaluate_persisted_incident
from pydantic import BaseModel, ConfigDict, Field

from .batch_jobs import (
    BatchEvaluationService,
    BatchJobConflict,
    BatchJobNotFound,
    BatchJobUnavailable,
)
from .batch_jobs import (
    service_from_environment as batch_service_from_environment,
)
from .catalog import SYMPTOMS, CatalogError, LabFault, Symptom, fault_view, load_catalog
from .evaluation_jobs import (
    AcknowledgementRequired,
    EvaluationJobService,
    JobConflict,
    JobNotFound,
    JobRejected,
    JobUnavailable,
    service_from_environment,
)
from .paging import notification
from .services import ServiceCheck, probe_url, service_checks, service_report

STATIC = Path(__file__).with_name("static")
PostJson = Callable[[str, dict[str, Any]], Any]
GetJson = Callable[[str], Any]


class PageBody(BaseModel):
    tool_budget: int = Field(default=10, ge=1, le=100)


class EvaluationJobRequest(BaseModel):
    """IDs and enums only. Paths, URLs, and PromQL are server-owned."""

    model_config = ConfigDict(extra="forbid")
    kind: str
    config_id: str
    case_ids: list[str] = Field(default_factory=list)
    acknowledgement: str | None = None


class RetrospectiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident_id: str = Field(min_length=1, max_length=200)
    case_id: str = Field(min_length=1, max_length=200)


class BatchEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    re_evaluate: bool = False


class UpstreamError(RuntimeError):
    """The incident API refused the page or could not be reached."""


def post_json(url: str, body: dict[str, Any], timeout: float = 10.0) -> Any:
    request = UrlRequest(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read(500).decode(errors="replace")
        raise UpstreamError(f"incident API returned HTTP {exc.code}: {detail}") from exc
    except (OSError, TimeoutError, URLError) as exc:
        raise UpstreamError(f"incident API request failed: {exc}") from exc
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UpstreamError("incident API returned non-JSON") from exc
    return parsed


def get_json(url: str, timeout: float = 10.0) -> Any:
    request = UrlRequest(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read(500).decode(errors="replace")
        raise UpstreamError(f"incident API returned HTTP {exc.code}: {detail}") from exc
    except (OSError, TimeoutError, URLError) as exc:
        raise UpstreamError(f"incident API request failed: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UpstreamError("incident API returned non-JSON") from exc


def alerts_endpoint(base: str, tool_budget: int) -> str:
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["tool_budget"] = str(tool_budget)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def incident_api_root(alerts_url: str) -> str:
    parts = urlsplit(alerts_url)
    path = parts.path.rstrip("/").removesuffix("/alerts")
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _incident_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("incident_id"), str):
        return payload["incident_id"]
    incidents = payload.get("incidents")
    if not isinstance(incidents, list) or not incidents or not isinstance(incidents[0], dict):
        return None
    incident_id = incidents[0].get("incident_id")
    return incident_id if isinstance(incident_id, str) else None


def _sse(event: str, payload: dict[str, Any], event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    body = json.dumps(payload, separators=(",", ":"))
    return f"{head}event: {event}\ndata: {body}\n\n"


def _event_cursor(value: str | None) -> int:
    try:
        return max(0, int(value or 0))
    except ValueError:
        return 0


def create_app(
    *,
    manifest_path: Path | None = None,
    flag_path: Path | None = None,
    alerts_url: str | None = None,
    console_url: str | None = None,
    evaluation_url: str | None = None,
    poster: PostJson | None = None,
    getter: GetJson | None = None,
    clock: Callable[[], str] | None = None,
    symptoms: dict[str, Symptom] | None = None,
    services: tuple[ServiceCheck, ...] | None = None,
    prober: Callable[[str], int | None] | None = None,
    jobs: EvaluationJobService | None = None,
    batch_jobs: BatchEvaluationService | None = None,
) -> FastAPI:
    manifest = manifest_path or Path(os.environ.get("ONCALL_MANIFEST", "eval/faults/manifest.yaml"))
    flags = flag_path or Path(os.environ.get("FLAGD_CONFIG", "deploy/flagd/demo.flagd.json"))
    alerts = alerts_url or os.environ.get("ONCALL_ALERTS_URL", "http://127.0.0.1:8000/alerts")
    console = console_url or os.environ.get("ONCALL_CONSOLE_URL", "http://127.0.0.1:8000/")
    evaluation = evaluation_url or os.environ.get("ONCALL_EVALUATION_URL", "http://127.0.0.1:8030/")
    send = poster or (lambda url, body: post_json(url, body))
    fetch = getter or get_json
    incident_api = os.environ.get("ONCALL_API_URL") or incident_api_root(alerts)
    timestamp = clock or _now
    published = SYMPTOMS if symptoms is None else symptoms
    watched = service_checks(os.environ) if services is None else services
    ask = prober or probe_url
    # Fault-catalog tests construct an app and never start an evaluation.
    # Opening the job database at import would write artifacts/ on every test.
    holder: dict[str, EvaluationJobService | None] = {"service": jobs}
    batch_holder: dict[str, BatchEvaluationService | None] = {"service": batch_jobs}

    def evaluation_jobs() -> EvaluationJobService:
        service = holder["service"]
        if service is None:
            service = service_from_environment()
            holder["service"] = service
        return service

    def incident_snapshots() -> list[dict[str, Any]]:
        payload = fetch(f"{incident_api}/api/incidents")
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise UpstreamError("incident API returned an invalid incident list")
        return payload

    def batch_evaluation_jobs() -> BatchEvaluationService:
        service = batch_holder["service"]
        if service is None:
            service = batch_service_from_environment(
                evaluation_jobs().settings,
                incident_snapshots,
            )
            batch_holder["service"] = service
        return service

    app = FastAPI(title="On-Call Copilot Lab", version="0.1.0")
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.exception_handler(CatalogError)
    async def catalog_error(_request, exc: CatalogError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(FlagFileError)
    async def flag_file_error(_request, exc: FlagFileError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(FlagEditError)
    async def flag_edit_error(_request, exc: FlagEditError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(UpstreamError)
    async def upstream_error(_request, exc: UpstreamError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    def catalog() -> tuple[LabFault, ...]:
        return load_catalog(manifest, published)

    def one(fault_id: str) -> LabFault:
        for fault in catalog():
            if fault.fault_id == fault_id:
                return fault
        raise HTTPException(status_code=404, detail=f"unknown fault {fault_id!r}")

    def view(fault: LabFault) -> dict[str, Any]:
        document = read_document(flags)
        variant = current_variant(document, fault.flag)
        return fault_view(fault, variant)

    def apply(fault: LabFault, variant: str) -> dict[str, Any]:
        document = read_document(flags)
        updated, changed = set_variant(document, fault.flag, variant)
        if changed:
            write_document(flags, updated)
        return {"changed": changed, "fault": fault_view(fault, variant)}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        catalog()
        read_document(flags)
        return {"status": "ok"}

    @app.get("/api/services")
    def list_services() -> dict[str, list]:
        return service_report(watched, ask)

    @app.get("/api/faults")
    def list_faults() -> dict[str, Any]:
        return {
            "topology": "c1_single",
            "tool_budget_default": 10,
            "tool_budget_min": 1,
            "tool_budget_max": 100,
            "console_url": console,
            "evaluation_url": evaluation,
            "faults": [view(fault) for fault in catalog()],
        }

    @app.post("/api/faults/{fault_id}/arm")
    def arm(fault_id: str) -> dict[str, Any]:
        fault = one(fault_id)
        return apply(fault, fault.arm_variant)

    @app.post("/api/faults/{fault_id}/disarm")
    def disarm(fault_id: str) -> dict[str, Any]:
        fault = one(fault_id)
        return apply(fault, fault.disarm_variant)

    @app.post("/api/faults/{fault_id}/page")
    def page(fault_id: str, body: PageBody) -> dict[str, Any]:
        fault = one(fault_id)
        if fault.symptom is None:
            raise HTTPException(
                status_code=422,
                detail=f"no symptom is published for {fault_id!r}",
            )
        alert = notification(fault.symptom, timestamp())
        payload = send(alerts_endpoint(alerts, body.tool_budget), alert)
        return {
            "fault_id": fault.fault_id,
            "tool_budget": body.tool_budget,
            "alert": alert,
            "incident_id": _incident_id(payload),
            "incident_api": payload,
        }

    @app.get("/api/evaluation-jobs/capabilities")
    def evaluation_job_capabilities() -> dict[str, Any]:
        return evaluation_jobs().capabilities()

    @app.get("/api/saved-incidents")
    def saved_incidents() -> dict[str, Any]:
        payload = incident_snapshots()
        evaluation_status = incident_evaluation_status(
            payload, evaluation_jobs().settings.artifacts_dir
        )
        rows = []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("incident_id"), str):
                continue
            labels = (item.get("alert") or {}).get("labels") or {}
            row = {
                "incident_id": item["incident_id"],
                "status": item.get("status"),
                "created_at": item.get("created_at"),
                "config_id": item.get("config_id"),
                "alertname": labels.get("alertname"),
                "service": labels.get("service"),
                "eligible": item.get("terminal") is True
                and isinstance(item.get("system_run"), dict),
            }
            row.update(
                evaluation_status.get(
                    item["incident_id"],
                    {"deterministic_evaluated": False, "llm_evaluated": False},
                )
            )
            rows.append(row)
        return {"incidents": rows}

    @app.get("/api/batch-evaluations/capabilities")
    def batch_evaluation_capabilities() -> dict[str, Any]:
        return batch_evaluation_jobs().capabilities()

    @app.post("/api/batch-evaluations", status_code=202)
    def start_batch_evaluation(body: BatchEvaluationRequest) -> dict[str, Any]:
        try:
            job = batch_evaluation_jobs().submit(re_evaluate=body.re_evaluate)
        except BatchJobConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except BatchJobUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return job.as_dict()

    @app.get("/api/batch-evaluations/{job_id}")
    def get_batch_evaluation(job_id: str) -> dict[str, Any]:
        service = batch_evaluation_jobs()
        try:
            job = service.store.get(job_id)
            events = service.store.events(job_id)
        except BatchJobNotFound as exc:
            raise HTTPException(status_code=404, detail="unknown batch evaluation job") from exc
        payload = job.as_dict()
        payload["events"] = [event.as_dict() for event in events]
        return payload

    @app.get("/api/batch-evaluations/{job_id}/stream")
    async def stream_batch_evaluation(
        job_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        service = batch_evaluation_jobs()
        try:
            service.store.get(job_id)
        except BatchJobNotFound as exc:
            raise HTTPException(status_code=404, detail="unknown batch evaluation job") from exc
        cursor = _event_cursor(last_event_id)

        async def events():
            nonlocal cursor
            current = service.store.get(job_id)
            snapshot = current.as_dict()
            snapshot["events"] = [event.as_dict() for event in service.store.events(job_id)]
            yield _sse("snapshot", snapshot)
            keepalive_at = time.monotonic() + service.keepalive_seconds
            while True:
                for event in service.store.events_after(job_id, cursor):
                    cursor = event.sequence
                    yield _sse("progress", event.as_dict(), event.sequence)
                    keepalive_at = time.monotonic() + service.keepalive_seconds
                current = service.store.get(job_id)
                if current.state in {"succeeded", "failed", "interrupted"}:
                    yield _sse("completed", current.as_dict(), cursor)
                    return
                if await request.is_disconnected():
                    return
                if time.monotonic() >= keepalive_at:
                    yield ": keepalive\n\n"
                    keepalive_at = time.monotonic() + service.keepalive_seconds
                await asyncio.sleep(min(0.2, service.keepalive_seconds))

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/evaluation-cases")
    def evaluation_cases() -> dict[str, Any]:
        cases = resolve_cases(evaluation_jobs().settings, ())
        return {
            "cases": [
                {
                    "case_id": case["case_id"],
                    "fault_id": case["fault_id"],
                    "complexity": case["complexity"],
                }
                for case in cases
            ]
        }

    @app.post("/api/retrospective-evaluations", status_code=201)
    def retrospective_evaluation(body: RetrospectiveRequest) -> dict[str, Any]:
        if not body.incident_id.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(status_code=400, detail="incident_id must be a plain identifier")
        try:
            case = resolve_cases(evaluation_jobs().settings, (body.case_id,))[0]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        incident = fetch(f"{incident_api}/incidents/{quote(body.incident_id, safe='')}")
        if not isinstance(incident, dict):
            raise UpstreamError("incident API returned invalid incident evidence")
        try:
            result = evaluate_persisted_incident(
                case,
                incident,
                evaluation_jobs().settings.artifacts_dir,
            )
        except RetrospectiveEvaluationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        run_id = result.record["run_id"]
        return {
            "run_id": run_id,
            "passed": result.record["passed"],
            "failure_reasons": result.record["failure_reasons"],
            "report_url": f"{evaluation.rstrip('/')}?run={quote(run_id, safe='')}",
        }

    @app.post("/api/evaluation-jobs", status_code=202)
    def start_evaluation_job(body: EvaluationJobRequest) -> dict[str, str]:
        try:
            job = evaluation_jobs().submit(
                kind=body.kind,
                config_id=body.config_id,
                case_ids=tuple(body.case_ids),
                acknowledgement=body.acknowledgement,
            )
        except AcknowledgementRequired as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except JobRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except JobConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except JobUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"job_id": job.job_id, "state": job.state}

    @app.get("/api/evaluation-jobs")
    def list_evaluation_jobs() -> dict[str, Any]:
        return {"jobs": [job.as_dict() for job in evaluation_jobs().store.list_jobs()]}

    @app.get("/api/evaluation-jobs/{job_id}")
    def get_evaluation_job(job_id: str) -> dict[str, Any]:
        service = evaluation_jobs()
        try:
            job = service.store.get(job_id)
            events = service.store.events(job_id)
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="unknown evaluation job") from exc
        payload = job.as_dict()
        payload["events"] = [item.as_dict() for item in events]
        return payload

    @app.get("/api/evaluation-jobs/{job_id}/stream")
    async def stream_evaluation_job(
        job_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        service = evaluation_jobs()
        try:
            service.store.get(job_id)
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="unknown evaluation job") from exc
        cursor = _event_cursor(last_event_id)

        async def events():
            nonlocal cursor
            current = service.store.get(job_id)
            snapshot = current.as_dict()
            snapshot["events"] = [item.as_dict() for item in service.store.events(job_id)]
            yield _sse("snapshot", snapshot)
            keepalive_at = time.monotonic() + service.keepalive_seconds
            while True:
                fresh = service.store.events_after(job_id, cursor)
                for item in fresh:
                    cursor = item.sequence
                    yield _sse(
                        "progress",
                        {
                            "sequence": item.sequence,
                            "phase": item.phase,
                            "message": item.message,
                        },
                        item.sequence,
                    )
                    keepalive_at = time.monotonic() + service.keepalive_seconds
                current = service.store.get(job_id)
                if current.state in {"succeeded", "failed", "interrupted"}:
                    yield _sse("completed", current.as_dict(), cursor)
                    return
                if await request.is_disconnected():
                    return
                if time.monotonic() >= keepalive_at:
                    yield ": keepalive\n\n"
                    keepalive_at = time.monotonic() + service.keepalive_seconds
                await asyncio.sleep(min(0.2, service.keepalive_seconds))

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run(
        "oncall_lab.main:app",
        host=os.environ.get("ONCALL_LAB_HOST", "127.0.0.1"),
        port=int(os.environ.get("ONCALL_LAB_PORT", "8020")),
        reload=False,
    )
