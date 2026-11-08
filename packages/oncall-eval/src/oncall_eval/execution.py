"""Typed evaluation runs shared by the CLI and the Control Center.

Paths, URLs, and PromQL live in ``EvaluationSettings``, which the caller
owns. A request only names a kind, a topology, and case ids.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from oncall_contracts.config import Topology

from .cases import EvalCase, load_case, validate_case
from .http_client import IncidentApiClient, IncidentApiError
from .injector import FaultInjectionError, FlagdFaultInjector
from .live_lock import live_evaluation_lock
from .manifest import load_manifest
from .runner import EvaluationRunner
from .signals import PollPolicy, PrometheusClient, SignalPoller, SignalReadError

Kind = Literal["preflight", "deterministic"]
_TOPOLOGIES = ("c0_workflow", "c1_single", "c2_team")
_PREFLIGHT_REFUSALS = (FaultInjectionError, IncidentApiError, SignalReadError)


class EvaluationRefused(Exception):
    """A deterministic run stopped during preflight, before any fault write."""

    def __init__(self, reason: BaseException) -> None:
        super().__init__(str(reason))
        self.reason = reason


@dataclass(frozen=True)
class EvaluationSettings:
    cases_dir: Path
    manifest_path: Path
    flagd_config: Path
    prometheus_url: str
    api_url: str
    artifacts_dir: Path
    lock_file: Path
    fail_under: float
    group_by: str | None = None
    run_timeout: float = 600.0
    api_poll_interval: float = 1.0
    signal_timeout: float = 120.0
    signal_interval: float = 5.0
    signal_successes: int = 2


@dataclass(frozen=True)
class EvaluationRequest:
    kind: Kind
    config_id: Topology
    case_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvaluationEvent:
    phase: str
    message: str
    run_id: str | None = None


@dataclass(frozen=True)
class EvaluationExecutionResult:
    passed: bool
    run_ids: tuple[str, ...]
    artifact_paths: tuple[Path, ...]


def execute_evaluation(
    request: EvaluationRequest,
    settings: EvaluationSettings,
    emit: Callable[[EvaluationEvent], None],
) -> EvaluationExecutionResult:
    """Run one preflight or one deterministic batch. The caller holds no shell."""
    if request.kind not in ("preflight", "deterministic"):
        raise ValueError(f"unknown evaluation kind {request.kind!r}")
    if request.config_id not in _TOPOLOGIES:
        raise ValueError(f"unknown topology {request.config_id!r}")
    cases, faults = load_validated_cases(settings.cases_dir, settings.manifest_path)
    cases = _select_cases(cases, request.case_ids)
    emit(EvaluationEvent("validating", f"validated {len(cases)} case(s)"))

    injector = FlagdFaultInjector(settings.flagd_config)
    prometheus = PrometheusClient(settings.prometheus_url)
    api_client = IncidentApiClient(
        settings.api_url,
        run_timeout_seconds=settings.run_timeout,
        poll_interval_seconds=settings.api_poll_interval,
    )
    with live_evaluation_lock(settings.lock_file):
        emit(
            EvaluationEvent("preflight", "checking the incident API, Prometheus, and flag baseline")
        )
        try:
            _preflight_live(cases, faults, request.config_id, injector, prometheus, api_client)
        except _PREFLIGHT_REFUSALS as exc:
            # The CLI labels only this failure as "before fault injection".
            # The same exception types during runner.run stay unwrapped.
            if request.kind == "deterministic":
                raise EvaluationRefused(exc) from exc
            raise
        if request.kind == "preflight":
            emit(
                EvaluationEvent(
                    "complete",
                    f"preflight PASS: {request.config_id} is advertised; API, Prometheus, "
                    "fault baseline, and live-evaluation lock are ready",
                )
            )
            return EvaluationExecutionResult(True, (), ())
        return _execute_live(
            request, settings, cases, faults, injector, prometheus, api_client, emit
        )


def resolve_cases(settings: EvaluationSettings, case_ids: tuple[str, ...]) -> list[EvalCase]:
    """Load the server-owned cases and keep ``case_ids``. An empty selection is all cases.

    This reads the case directory and the manifest. It does not open the flag file.
    """
    cases, _faults = load_validated_cases(settings.cases_dir, settings.manifest_path)
    return _select_cases(cases, case_ids)


def _select_cases(cases: list[EvalCase], case_ids: tuple[str, ...]) -> list[EvalCase]:
    if not case_ids:
        return cases
    known = {case["case_id"]: case for case in cases}
    missing = [case_id for case_id in case_ids if case_id not in known]
    if missing:
        raise ValueError("unknown case ids: " + ", ".join(missing))
    return [known[case_id] for case_id in case_ids]


def load_validated_cases(
    cases_dir: Path, manifest_path: Path
) -> tuple[list[EvalCase], dict[str, Any]]:
    faults = load_manifest(manifest_path)
    paths = sorted(cases_dir.glob("*.json"))
    if not paths:
        raise ValueError(f"no case files found in {cases_dir}")
    cases = []
    problems = []
    for path in paths:
        case = load_case(path)
        if issues := validate_case(case, faults):
            problems.extend(f"{path.name}: {issue}" for issue in issues)
        else:
            cases.append(case)
    if problems:
        raise ValueError("invalid evaluation cases:\n" + "\n".join(problems))
    return cases, faults


def _preflight_live(
    cases: list[EvalCase],
    faults: dict[str, Any],
    config_id: Topology,
    injector: FlagdFaultInjector,
    prometheus: PrometheusClient,
    api_client: IncidentApiClient,
) -> None:
    """Prove dependencies and fault baselines before mutating the lab."""
    api_client.preflight(config_id)
    checked: set[str] = set()
    for case in cases:
        fault = faults[case["fault_id"]]
        if fault["fault_id"] in checked:
            continue
        checked.add(fault["fault_id"])
        disable_args = fault["disable"]["args"]
        flag = disable_args["flag"]
        baseline = disable_args["variant"]
        current = injector.current_variant(flag)
        if current != baseline:
            raise FaultInjectionError(
                f"preflight expected {flag!r} baseline {baseline!r}, found {current!r}"
            )
        prometheus.sample(fault["detect"]["query"])
        prometheus.sample(fault["recovery"]["query"])


def _execute_live(
    request: EvaluationRequest,
    settings: EvaluationSettings,
    cases: list[EvalCase],
    faults: dict[str, Any],
    injector: FlagdFaultInjector,
    prometheus: PrometheusClient,
    api_client: IncidentApiClient,
    emit: Callable[[EvaluationEvent], None],
) -> EvaluationExecutionResult:
    poller = SignalPoller(
        prometheus,
        PollPolicy(
            timeout_seconds=settings.signal_timeout,
            interval_seconds=settings.signal_interval,
            consecutive_successes=settings.signal_successes,
        ),
    )
    runner = EvaluationRunner(
        injector=injector,
        poller=poller,
        system=api_client,
        artifacts_dir=settings.artifacts_dir,
    )
    passed = 0
    run_ids: list[str] = []
    paths: list[Path] = []
    grouped: dict[str, list[bool]] = defaultdict(list)
    emit(
        EvaluationEvent(
            "injecting",
            f"live mode: running {len(cases)} case(s) against {request.config_id}",
        )
    )
    for case in cases:
        result = runner.run(case, faults[case["fault_id"]], request.config_id)
        run_id = str(result.record["run_id"])
        run_ids.append(run_id)
        paths.append(result.artifact_path)
        passed += int(result.score.passed)
        grouped[case["complexity"]].append(result.score.passed)
        suffix = "" if result.score.passed else f" — {'; '.join(result.score.reasons)}"
        emit(
            EvaluationEvent(
                "scoring",
                f"  {case['case_id']}: {'PASS' if result.score.passed else 'FAIL'}{suffix}",
                run_id,
            )
        )
        emit(EvaluationEvent("scoring", f"    record: {result.artifact_path}", run_id))

    if settings.group_by == "complexity":
        emit(EvaluationEvent("scoring", "by complexity:"))
        for name, values in sorted(grouped.items()):
            emit(EvaluationEvent("scoring", f"  {name}: {sum(values)}/{len(values)}"))
    rate = passed / len(cases)
    emit(
        EvaluationEvent(
            "complete",
            f"pass rate: {passed}/{len(cases)} = {rate:.3f}; required {settings.fail_under:.3f}",
        )
    )
    return EvaluationExecutionResult(rate >= settings.fail_under, tuple(run_ids), tuple(paths))
