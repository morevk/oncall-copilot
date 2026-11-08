"""Batch retrospective deterministic and LLM evaluation over saved incidents."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oncall_contracts.runrecord import RunRecord

from .cases import EvalCase
from .judge import JudgeResult, JudgeSettings, judge_run
from .retrospective import evaluate_persisted_incident
from .rubric import RubricRecord, load_rubric_record
from .runner import load_run_record

BatchEmit = Callable[[str, str], None]
Judge = Callable[[RunRecord, Mapping[str, Any], JudgeSettings], JudgeResult]


@dataclass(frozen=True)
class BatchEvaluationSummary:
    eligible: int
    deterministic_created: int
    llm_created: int
    skipped_completed: int
    skipped_no_case: int
    failed: int
    run_ids: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.failed == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "deterministic_created": self.deterministic_created,
            "llm_created": self.llm_created,
            "skipped_completed": self.skipped_completed,
            "skipped_no_case": self.skipped_no_case,
            "failed": self.failed,
            "run_ids": list(self.run_ids),
        }


def evaluate_saved_incidents(
    incidents: Sequence[Mapping[str, Any]],
    cases: Sequence[EvalCase],
    runs_dir: Path,
    judge_settings: JudgeSettings,
    *,
    re_evaluate: bool = False,
    emit: BatchEmit | None = None,
    judge: Judge = judge_run,
) -> BatchEvaluationSummary:
    """Fill missing deterministic/judge artifacts, or append a fresh pair when forced."""
    notify = emit or (lambda _phase, _message: None)
    runs_by_incident, rubrics_by_run = _existing_artifacts(runs_dir)
    eligible = 0
    deterministic_created = 0
    llm_created = 0
    skipped_completed = 0
    skipped_no_case = 0
    failed = 0
    produced: list[str] = []

    for incident in incidents:
        incident_id = incident.get("incident_id")
        if (
            not isinstance(incident_id, str)
            or incident.get("terminal") is not True
            or not isinstance(incident.get("system_run"), Mapping)
        ):
            continue
        eligible += 1
        matched = _matching_cases(cases, incident)
        if len(matched) != 1:
            skipped_no_case += 1
            reason = "no matching case" if not matched else "multiple matching cases"
            notify("skipped", f"{incident_id}: {reason}; deterministic scoring was not guessed")
            continue

        existing = list(runs_by_incident.get(incident_id, ()))
        completed = [
            run
            for run in existing
            if (rubric := rubrics_by_run.get(run["run_id"])) is not None
            and rubric["scorer_type"] == "llm"
        ]
        if completed and not re_evaluate:
            skipped_completed += 1
            notify("skipped", f"{incident_id}: deterministic and LLM evaluations already exist")
            continue

        run: RunRecord | None = None
        if not re_evaluate:
            # Prefer a deterministic artifact that has no rubric. A human rubric occupies
            # the one-rubric-per-run slot, so preserve it and create a fresh run below.
            run = next(
                (item for item in reversed(existing) if item["run_id"] not in rubrics_by_run),
                None,
            )
        try:
            if run is None:
                result = evaluate_persisted_incident(matched[0], incident, runs_dir)
                run = result.record
                runs_by_incident[incident_id].append(run)
                deterministic_created += 1
                produced.append(run["run_id"])
                notify("deterministic", f"{incident_id}: deterministic artifact created")
            judge_result = judge(run, incident, judge_settings)
            rubrics_by_run[run["run_id"]] = judge_result.record
            llm_created += 1
            if run["run_id"] not in produced:
                produced.append(run["run_id"])
            notify("judge", f"{incident_id}: LLM rubric created")
        except (OSError, RuntimeError, ValueError) as exc:
            failed += 1
            notify("failed", f"{incident_id}: {exc}")

    return BatchEvaluationSummary(
        eligible=eligible,
        deterministic_created=deterministic_created,
        llm_created=llm_created,
        skipped_completed=skipped_completed,
        skipped_no_case=skipped_no_case,
        failed=failed,
        run_ids=tuple(produced),
    )


def incident_evaluation_status(
    incidents: Sequence[Mapping[str, Any]], runs_dir: Path
) -> dict[str, dict[str, bool]]:
    """Return deterministic/LLM completion flags keyed by incident id."""
    runs_by_incident, rubrics_by_run = _existing_artifacts(runs_dir)
    result = {}
    for incident in incidents:
        incident_id = incident.get("incident_id")
        if not isinstance(incident_id, str):
            continue
        runs = runs_by_incident.get(incident_id, ())
        result[incident_id] = {
            "deterministic_evaluated": bool(runs),
            "llm_evaluated": any(
                (rubric := rubrics_by_run.get(run["run_id"])) is not None
                and rubric["scorer_type"] == "llm"
                for run in runs
            ),
        }
    return result


def _existing_artifacts(
    runs_dir: Path,
) -> tuple[defaultdict[str, list[RunRecord]], dict[str, RubricRecord]]:
    runs_by_incident: defaultdict[str, list[RunRecord]] = defaultdict(list)
    for path in sorted(runs_dir.glob("*.json"), key=lambda item: item.stat().st_mtime_ns):
        run = load_run_record(path)
        incident_id = run.get("source_incident_id") or f"eval-{run['run_id']}"
        runs_by_incident[incident_id].append(run)
    rubrics = {}
    for path in sorted((runs_dir.parent / "rubrics").glob("*.json")):
        rubric = load_rubric_record(path)
        rubrics[rubric["run_id"]] = rubric
    return runs_by_incident, rubrics


def _matching_cases(cases: Sequence[EvalCase], incident: Mapping[str, Any]) -> list[EvalCase]:
    alert = incident.get("alert")
    if not isinstance(alert, Mapping):
        return []
    labels = alert.get("labels")
    if not isinstance(labels, Mapping):
        return []
    matches = []
    for case in cases:
        expected = case["alert"].get("labels") or {}
        if all(
            expected.get(label) is None or labels.get(label) == expected[label]
            for label in ("alertname", "service")
        ):
            matches.append(case)
    return matches
