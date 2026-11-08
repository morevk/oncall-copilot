"""Control Center evaluation jobs: persistence, acknowledgement, and SSE."""

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from oncall_eval.execution import (
    EvaluationEvent,
    EvaluationExecutionResult,
    EvaluationSettings,
)
from oncall_eval.live_lock import live_evaluation_lock
from oncall_lab.evaluation_jobs import (
    EvaluationJobService,
    JobConflict,
    JobStore,
    jobs_database_from_environment,
    settings_from_environment,
)
from oncall_lab.main import create_app

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "eval" / "faults" / "manifest.yaml"
CASES = ROOT / "eval" / "dev"


def _settings(tmp_path: Path, *, cases: Path | None = None) -> EvaluationSettings:
    return EvaluationSettings(
        cases_dir=cases or CASES,
        manifest_path=MANIFEST,
        flagd_config=tmp_path / "demo.flagd.json",
        prometheus_url="http://prometheus.internal:9090",
        api_url="http://oncall.internal:8000",
        artifacts_dir=tmp_path / "runs",
        lock_file=tmp_path / "locks" / "live-evaluation.lock",
        fail_under=1.0,
    )


def _passed(run_id: str = "run-123") -> EvaluationExecutionResult:
    return EvaluationExecutionResult(True, (run_id,), (Path(f"{run_id}.json"),))


def _service(tmp_path: Path, execute, **kwargs) -> EvaluationJobService:
    return EvaluationJobService(
        tmp_path / "evaluation-jobs.sqlite3",
        _settings(tmp_path, cases=kwargs.get("cases")),
        execute=execute,
        clock=lambda: "2026-11-08T00:00:00Z",
        keepalive_seconds=kwargs.get("keepalive_seconds", 15.0),
        lock_held=kwargs.get("lock_held"),
    )


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    found = []
    for block in text.split("\n\n"):
        stripped = block.strip()
        if not stripped:
            continue
        if stripped.startswith(":"):
            found.append(("keepalive", {}))
            continue
        event = "message"
        data = ""
        for line in stripped.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        found.append((event, json.loads(data) if data else {}))
    return found


def test_restart_interrupts_an_unfinished_job_and_does_not_resume(tmp_path: Path):
    database = tmp_path / "evaluation-jobs.sqlite3"
    store = JobStore(database, clock=lambda: "2026-11-08T00:00:00Z")
    job = store.create(
        kind="deterministic",
        config_id="c1_single",
        case_ids=("dev-001",),
        lock_held=lambda: False,
    )
    assert store.mark_running(job.job_id) is True
    store.append_event(job.job_id, "injecting", "live mode: running 1 case(s)")

    restarted = JobStore(database, clock=lambda: "2026-11-08T00:01:00Z")
    revived = restarted.get(job.job_id)
    assert revived.state == "interrupted"
    assert revived.finished_at == "2026-11-08T00:01:00Z"
    assert "not resumed" in revived.message
    assert restarted.mark_running(job.job_id) is False
    assert restarted.active_job_id() is None

    successor = restarted.create(
        kind="preflight",
        config_id="c1_single",
        case_ids=("dev-001",),
        lock_held=lambda: False,
    )
    assert successor.state == "queued"
    assert successor.job_id != job.job_id


def test_a_second_active_job_is_rejected(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.create(
        kind="preflight",
        config_id="c1_single",
        case_ids=(),
        lock_held=lambda: False,
    )
    with pytest.raises(JobConflict, match="already active"):
        store.create(
            kind="preflight",
            config_id="c1_single",
            case_ids=(),
            lock_held=lambda: False,
        )


def test_job_database_stays_separate_from_the_dashboard_index(monkeypatch):
    for name in (
        "ONCALL_EVAL_JOBS_DATABASE",
        "ONCALL_EVAL_ARTIFACTS",
        "ONCALL_EVAL_LOCK_FILE",
        "ONCALL_EVAL_CASES",
        "ONCALL_API_URL",
        "ONCALL_SERVICE_URL",
        "PROMETHEUS_URL",
        "FLAGD_CONFIG",
        "ONCALL_MANIFEST",
    ):
        monkeypatch.delenv(name, raising=False)

    assert jobs_database_from_environment().name == "evaluation-jobs.sqlite3"
    assert jobs_database_from_environment().name != "evaluation.sqlite3"
    settings = settings_from_environment()
    assert settings.artifacts_dir == Path("artifacts") / "runs"
    assert settings.cases_dir == Path("eval/dev")
    assert settings.prometheus_url == "http://127.0.0.1:9090"


def test_preflight_needs_no_acknowledgement_and_hides_server_paths(tmp_path: Path):
    seen = []

    def execute(request, settings, emit):
        seen.append(request)
        emit(EvaluationEvent("validating", "validated 1 case(s)"))
        emit(EvaluationEvent("complete", "preflight PASS: ready"))
        return _passed()

    service = _service(tmp_path, execute)
    app = create_app(
        manifest_path=MANIFEST,
        flag_path=tmp_path / "unused-flags.json",
        poster=lambda _url, _body: {},
        jobs=service,
    )
    with TestClient(app) as client:
        started = client.post(
            "/api/evaluation-jobs",
            json={"kind": "preflight", "config_id": "c1_single", "case_ids": ["dev-001"]},
        )
        assert started.status_code == 202
        assert started.json()["state"] == "queued"
        service.join()
        body = client.get(f"/api/evaluation-jobs/{started.json()['job_id']}").json()

    assert seen[0].kind == "preflight"
    assert seen[0].case_ids == ("dev-001",)
    assert body["state"] == "succeeded"
    assert body["run_ids"] == ["run-123"]
    rendered = json.dumps(body)
    assert "prometheus.internal" not in rendered
    assert "demo.flagd.json" not in rendered
    assert not (tmp_path / "demo.flagd.json").exists()


def test_live_start_rejects_an_inexact_acknowledgement(tmp_path: Path):
    called = []

    def execute(request, settings, emit):
        called.append(request)
        emit(EvaluationEvent("complete", "pass rate: 1/1 = 1.000; required 1.000"))
        return _passed()

    service = _service(tmp_path, execute)
    app = create_app(
        manifest_path=MANIFEST,
        flag_path=tmp_path / "flags.json",
        poster=lambda _url, _body: {},
        jobs=service,
    )
    with TestClient(app) as client:
        missing = client.post(
            "/api/evaluation-jobs",
            json={"kind": "deterministic", "config_id": "c1_single", "case_ids": ["dev-001"]},
        )
        wrong_case = client.post(
            "/api/evaluation-jobs",
            json={
                "kind": "deterministic",
                "config_id": "c1_single",
                "case_ids": ["dev-001"],
                "acknowledgement": "inject live faults",
            },
        )
        accepted = client.post(
            "/api/evaluation-jobs",
            json={
                "kind": "deterministic",
                "config_id": "c1_single",
                "case_ids": ["dev-001"],
                "acknowledgement": "INJECT LIVE FAULTS",
            },
        )
        service.join()

    assert missing.status_code == 422
    assert wrong_case.status_code == 422
    assert "INJECT LIVE FAULTS" in missing.json()["detail"]
    assert accepted.status_code == 202
    assert len(called) == 1


def test_browser_cannot_supply_paths_or_unknown_ids(tmp_path: Path):
    called = []
    service = _service(tmp_path, lambda *args: called.append(args))
    app = create_app(
        manifest_path=MANIFEST,
        flag_path=tmp_path / "flags.json",
        poster=lambda _url, _body: {},
        jobs=service,
    )
    with TestClient(app) as client:
        paths = client.post(
            "/api/evaluation-jobs",
            json={
                "kind": "preflight",
                "config_id": "c1_single",
                "cases_dir": "/tmp/cases",
                "prometheus_url": "http://evil",
            },
        )
        unknown_case = client.post(
            "/api/evaluation-jobs",
            json={"kind": "preflight", "config_id": "c1_single", "case_ids": ["not-a-case"]},
        )
        unknown_topology = client.post(
            "/api/evaluation-jobs",
            json={"kind": "preflight", "config_id": "c2_team", "case_ids": ["dev-001"]},
        )
        slash = client.post(
            "/api/evaluation-jobs",
            json={"kind": "preflight", "config_id": "c1_single", "case_ids": ["../dev-001"]},
        )

    assert paths.status_code == 422
    assert unknown_case.status_code == 400
    assert "unknown case ids" in unknown_case.json()["detail"]
    assert unknown_topology.status_code == 400
    assert slash.status_code == 400
    assert called == []
    assert service.store.list_jobs() == ()


def test_missing_cases_are_unavailable_and_a_held_lock_conflicts(tmp_path: Path):
    called = []
    empty = tmp_path / "empty-cases"
    empty.mkdir()
    unavailable = _service(
        tmp_path / "unavailable",
        lambda *args: called.append(args),
        cases=empty,
    )
    held = _service(
        tmp_path / "held",
        lambda *args: called.append("held"),
        lock_held=lambda: True,
    )
    with TestClient(
        create_app(manifest_path=MANIFEST, flag_path=tmp_path / "a.json", jobs=unavailable)
    ) as client:
        missing = client.post(
            "/api/evaluation-jobs",
            json={"kind": "preflight", "config_id": "c1_single"},
        )
    with TestClient(
        create_app(manifest_path=MANIFEST, flag_path=tmp_path / "b.json", jobs=held)
    ) as client:
        locked = client.post(
            "/api/evaluation-jobs",
            json={"kind": "preflight", "config_id": "c1_single", "case_ids": ["dev-001"]},
        )

    assert missing.status_code == 503
    assert locked.status_code == 409
    assert "another live evaluation" in locked.json()["detail"]
    assert called == []


def test_advisory_lock_rejects_a_second_process(tmp_path: Path):
    called = []
    service = _service(tmp_path, lambda *args: called.append(args))
    app = create_app(manifest_path=MANIFEST, flag_path=tmp_path / "flags.json", jobs=service)
    with live_evaluation_lock(service.settings.lock_file), TestClient(app) as client:
        response = client.post(
            "/api/evaluation-jobs",
            json={"kind": "preflight", "config_id": "c1_single", "case_ids": ["dev-001"]},
        )

    assert response.status_code == 409
    assert called == []


def test_a_running_job_rejects_the_next_start(tmp_path: Path):
    started = threading.Event()
    release = threading.Event()

    def execute(request, settings, emit):
        emit(EvaluationEvent("validating", "validated 1 case(s)"))
        started.set()
        assert release.wait(timeout=3)
        emit(EvaluationEvent("complete", "preflight PASS: ready"))
        return _passed()

    service = _service(tmp_path, execute)
    app = create_app(manifest_path=MANIFEST, flag_path=tmp_path / "flags.json", jobs=service)
    with TestClient(app) as client:
        first = client.post(
            "/api/evaluation-jobs",
            json={"kind": "preflight", "config_id": "c1_single", "case_ids": ["dev-001"]},
        )
        assert first.status_code == 202
        assert started.wait(timeout=2)
        second = client.post(
            "/api/evaluation-jobs",
            json={"kind": "preflight", "config_id": "c1_single", "case_ids": ["dev-001"]},
        )
        capabilities = client.get("/api/evaluation-jobs/capabilities").json()
        release.set()
        service.join()

    assert second.status_code == 409
    assert "already active" in second.json()["detail"]
    assert capabilities["active_job_id"] == first.json()["job_id"]
    assert capabilities["judge_available"] is False
    assert capabilities["topologies"] == ["c1_single"]


def test_unexpected_failures_keep_a_short_message(tmp_path: Path):
    def execute(request, settings, emit):
        raise RuntimeError("boom sk-secret traceback line")

    service = _service(tmp_path, execute)
    app = create_app(manifest_path=MANIFEST, flag_path=tmp_path / "flags.json", jobs=service)
    with TestClient(app) as client:
        started = client.post(
            "/api/evaluation-jobs",
            json={"kind": "preflight", "config_id": "c1_single", "case_ids": ["dev-001"]},
        )
        service.join()
        body = client.get(f"/api/evaluation-jobs/{started.json()['job_id']}").json()

    assert body["state"] == "failed"
    assert body["error"] == "evaluation failed before completion"
    assert "sk-secret" not in json.dumps(body)


def test_failed_score_still_records_the_run_id(tmp_path: Path):
    def execute(request, settings, emit):
        emit(EvaluationEvent("scoring", "dev-001: FAIL", "run-9"))
        emit(EvaluationEvent("complete", "pass rate: 0/1 = 0.000; required 1.000"))
        return EvaluationExecutionResult(False, ("run-9",), ())

    service = _service(tmp_path, execute)
    app = create_app(manifest_path=MANIFEST, flag_path=tmp_path / "flags.json", jobs=service)
    with TestClient(app) as client:
        started = client.post(
            "/api/evaluation-jobs",
            json={"kind": "preflight", "config_id": "c1_single", "case_ids": ["dev-001"]},
        )
        service.join()
        body = client.get(f"/api/evaluation-jobs/{started.json()['job_id']}").json()

    assert body["state"] == "failed"
    assert body["run_ids"] == ["run-9"]
    assert "0/1" in body["message"]


def test_stream_sends_snapshot_progress_keepalive_and_one_terminal_event(tmp_path: Path):
    def execute(request, settings, emit):
        emit(EvaluationEvent("validating", "validated 1 case(s)"))
        time.sleep(0.45)
        emit(EvaluationEvent("complete", "preflight PASS: ready"))
        return _passed("run-stream")

    service = _service(tmp_path, execute, keepalive_seconds=0.05)
    app = create_app(manifest_path=MANIFEST, flag_path=tmp_path / "flags.json", jobs=service)
    with TestClient(app) as client:
        started = client.post(
            "/api/evaluation-jobs",
            json={"kind": "preflight", "config_id": "c1_single", "case_ids": ["dev-001"]},
        )
        job_id = started.json()["job_id"]
        with client.stream("GET", f"/api/evaluation-jobs/{job_id}/stream", timeout=5) as response:
            assert response.status_code == 200
            chunks = []
            for chunk in response.iter_text():
                chunks.append(chunk)
                if "event: completed" in chunk:
                    break
        service.join()

    events = _parse_sse("".join(chunks))
    names = [name for name, _payload in events]
    assert names[0] == "snapshot"
    assert "progress" in names
    assert "keepalive" in names
    assert names.count("completed") == 1
    assert names[-1] == "completed"
    progress = [payload for name, payload in events if name == "progress"]
    assert [item["phase"] for item in progress] == ["validating", "complete"]
    completed = events[-1][1]
    assert completed["state"] == "succeeded"
    assert completed["run_ids"] == ["run-stream"]
    assert "prometheus.internal" not in json.dumps(completed)


def test_stream_reconnect_skips_events_the_client_already_has(tmp_path: Path):
    def execute(request, settings, emit):
        emit(EvaluationEvent("validating", "validated 1 case(s)"))
        emit(EvaluationEvent("complete", "preflight PASS: ready"))
        return _passed()

    service = _service(tmp_path, execute)
    app = create_app(manifest_path=MANIFEST, flag_path=tmp_path / "flags.json", jobs=service)
    with TestClient(app) as client:
        started = client.post(
            "/api/evaluation-jobs",
            json={"kind": "preflight", "config_id": "c1_single", "case_ids": ["dev-001"]},
        )
        job_id = started.json()["job_id"]
        service.join()
        first_sequence = client.get(f"/api/evaluation-jobs/{job_id}").json()["events"][0][
            "sequence"
        ]
        with client.stream(
            "GET",
            f"/api/evaluation-jobs/{job_id}/stream",
            headers={"Last-Event-ID": str(first_sequence)},
            timeout=5,
        ) as response:
            body = "".join(response.iter_text())

    events = _parse_sse(body)
    progress = [payload for name, payload in events if name == "progress"]
    assert [item["sequence"] for item in progress] == [first_sequence + 1]
    assert [item["phase"] for item in progress] == ["complete"]
    assert events[0][0] == "snapshot"
    assert events[-1][0] == "completed"
    assert sum(name == "completed" for name, _payload in events) == 1
