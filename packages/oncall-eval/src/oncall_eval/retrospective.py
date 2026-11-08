"""Truthful deterministic scoring of a completed, persisted incident."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast, get_args

from oncall_contracts.config import Topology

from .cases import EvalCase
from .runner import (
    EvaluationResult,
    build_run_record,
    validate_system_run,
    write_run_record,
)
from .scoring import SystemRun, score_run


class RetrospectiveEvaluationError(ValueError):
    """The saved incident cannot be scored against the selected case."""


def evaluate_persisted_incident(
    case: EvalCase,
    incident: Mapping[str, Any],
    artifacts_dir: Path,
    *,
    id_factory=None,
) -> EvaluationResult:
    """Create a non-mutating RunRecord from durable incident evidence.

    Historical instant telemetry cannot prove what happened at recovery time, so
    independent recovery and cleanup witnesses are deliberately false.
    """
    incident_id = incident.get("incident_id")
    if not isinstance(incident_id, str) or not incident_id:
        raise RetrospectiveEvaluationError("saved incident has no incident_id")
    if incident.get("terminal") is not True:
        raise RetrospectiveEvaluationError("incident must be terminal before evaluation")
    config_id = incident.get("config_id")
    if config_id not in get_args(Topology):
        raise RetrospectiveEvaluationError(f"incident has unknown config_id {config_id!r}")
    _match_alert(case, incident.get("alert"))
    raw_run = incident.get("system_run")
    if not isinstance(raw_run, Mapping):
        raise RetrospectiveEvaluationError("terminal incident has no system_run evidence")
    output = cast(SystemRun, dict(raw_run))
    try:
        validate_system_run(output)
    except (KeyError, TypeError, ValueError) as exc:
        raise RetrospectiveEvaluationError(f"invalid system_run evidence: {exc}") from exc

    score = score_run(case, output, independently_verified_recovery=False)
    run_id = (id_factory or (lambda: f"retrospective-{uuid.uuid4().hex}"))()
    if not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id:
        raise RetrospectiveEvaluationError("generated run id is not filename-safe")
    record = build_run_record(
        run_id=run_id,
        case=case,
        config_id=config_id,
        output=output,
        score=score,
        latency_ms=_latency_ms(incident),
        observation=None,
        source_incident_id=incident_id,
    )
    artifact_path = artifacts_dir / f"{run_id}.json"
    write_run_record(artifact_path, record)
    return EvaluationResult(record, score, artifact_path, None)


def _match_alert(case: EvalCase, value: Any) -> None:
    if not isinstance(value, Mapping):
        raise RetrospectiveEvaluationError("saved incident has no alert evidence")
    expected_labels = case["alert"].get("labels") or {}
    actual_labels = value.get("labels")
    if not isinstance(actual_labels, Mapping):
        raise RetrospectiveEvaluationError("saved incident alert has no labels")
    for label in ("alertname", "service"):
        expected = expected_labels.get(label)
        if expected is not None and actual_labels.get(label) != expected:
            raise RetrospectiveEvaluationError(
                f"incident alert {label} does not match case {case['case_id']!r}"
            )


def _latency_ms(incident: Mapping[str, Any]) -> int:
    try:
        created = datetime.fromisoformat(str(incident["created_at"]))
        updated = datetime.fromisoformat(str(incident["updated_at"]))
    except (KeyError, TypeError, ValueError):
        return 0
    return max(0, round((updated - created).total_seconds() * 1000))
