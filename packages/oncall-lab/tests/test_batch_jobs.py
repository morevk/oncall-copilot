from pathlib import Path

import pytest
from oncall_eval.batch import BatchEvaluationSummary
from oncall_eval.execution import EvaluationSettings
from oncall_eval.judge import JudgeConfigurationError, JudgeSettings
from oncall_lab.batch_jobs import (
    BatchEvaluationService,
    BatchJobStore,
    BatchJobUnavailable,
)

ROOT = Path(__file__).parents[3]


def _settings(tmp_path: Path) -> EvaluationSettings:
    return EvaluationSettings(
        cases_dir=ROOT / "eval/dev",
        manifest_path=ROOT / "eval/faults/manifest.yaml",
        flagd_config=tmp_path / "flags.json",
        prometheus_url="http://prometheus.test",
        api_url="http://oncall.test",
        artifacts_dir=tmp_path / "runs",
        lock_file=tmp_path / "live.lock",
        fail_under=1.0,
    )


def _judge_settings(tmp_path: Path) -> JudgeSettings:
    return JudgeSettings(
        model="google/test-judge",
        api_key="test-key",
        rubric_path=ROOT / "eval/rubrics/investigation-v1.yaml",
        artifacts_dir=tmp_path,
    )


def test_batch_job_runs_in_background_and_persists_progress(tmp_path: Path):
    seen = {}

    def execute(incidents, cases, runs_dir, judge_settings, *, re_evaluate, emit):
        seen.update(
            incidents=incidents,
            cases=cases,
            runs_dir=runs_dir,
            judge_settings=judge_settings,
            re_evaluate=re_evaluate,
        )
        emit("deterministic", "saved-001: deterministic artifact created")
        emit("judge", "saved-001: LLM rubric created")
        return BatchEvaluationSummary(1, 1, 1, 0, 0, 0, ("run-001",))

    service = BatchEvaluationService(
        tmp_path / "jobs.sqlite3",
        _settings(tmp_path),
        lambda: [{"incident_id": "saved-001"}],
        judge_settings=lambda: _judge_settings(tmp_path),
        execute=execute,
        keepalive_seconds=0.01,
    )

    queued = service.submit(re_evaluate=True)
    service.join()
    finished = service.store.get(queued.job_id)

    assert finished.state == "succeeded"
    assert finished.re_evaluate is True
    assert finished.summary["llm_created"] == 1
    assert [event.phase for event in service.store.events(queued.job_id)] == [
        "deterministic",
        "judge",
        "complete",
    ]
    assert seen["re_evaluate"] is True
    assert seen["runs_dir"] == tmp_path / "runs"


def test_batch_capability_and_submit_fail_closed_without_judge(tmp_path: Path):
    def unavailable():
        raise JudgeConfigurationError("JUDGE_MODEL is not configured")

    service = BatchEvaluationService(
        tmp_path / "jobs.sqlite3",
        _settings(tmp_path),
        list,
        judge_settings=unavailable,
    )

    assert service.capabilities()["judge_available"] is False
    with pytest.raises(BatchJobUnavailable, match="JUDGE_MODEL"):
        service.submit(re_evaluate=False)


def test_unfinished_batch_is_marked_interrupted_on_restart(tmp_path: Path):
    path = tmp_path / "jobs.sqlite3"
    store = BatchJobStore(path)
    job = store.create(re_evaluate=False)
    assert store.mark_running(job.job_id) is True

    restored = BatchJobStore(path)

    assert restored.get(job.job_id).state == "interrupted"
    assert restored.events(job.job_id)[-1].phase == "interrupted"
