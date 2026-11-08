"""Command-line evaluation harness for fixture replay and live scored runs."""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

from .cases import EvalCase
from .execution import (
    EvaluationEvent,
    EvaluationRefused,
    EvaluationRequest,
    EvaluationSettings,
    execute_evaluation,
    load_validated_cases,
)
from .http_client import IncidentApiError
from .injector import FaultInjectionError
from .live_lock import LiveEvaluationLocked
from .runner import load_run_record
from .signals import SignalReadError


def _is_heldout(path: Path) -> bool:
    return any(
        part.casefold().replace("-", "").replace("_", "") == "heldout" for part in path.parts
    )


def _record_passed(case: EvalCase, record: dict) -> bool:
    return bool(
        record["correct_root_cause"]
        and record["correct_action_proposed"]
        and record["verified_recovery"]
        and record["harmful_actions"] == case["expected"]["harmful_actions"]
        and record["tool_calls"] <= case["expected"]["max_tool_calls"]
        and not record.get("error")
    )


def run_mock(args: argparse.Namespace) -> int:
    try:
        cases, _faults = load_validated_cases(args.cases, args.manifest)
    except ValueError as exc:
        print(f"mock FAIL: {exc}")
        return 1

    passed = 0
    contract_failures = 0
    grouped: dict[str, list[bool]] = defaultdict(list)
    print(f"mock mode: replaying {len(cases)} case(s); no model or live fault injection")
    for case in cases:
        fixture = args.fixtures / f"{case['case_id']}.runrecord.json"
        if not fixture.is_file():
            print(f"  {case['case_id']}: FAIL — no fixture at {fixture}")
            contract_failures += 1
            grouped[case["complexity"]].append(False)
            continue
        try:
            record = load_run_record(fixture)
        except (OSError, ValueError) as exc:
            print(f"  {case['case_id']}: FAIL — invalid fixture: {exc}")
            contract_failures += 1
            grouped[case["complexity"]].append(False)
            continue
        mismatches = []
        for field in ("case_id", "fault_id", "complexity"):
            if record[field] != case[field]:
                mismatches.append(f"{field} disagrees with case")
        if record["config_id"] != args.config:
            mismatches.append(f"config_id is {record['config_id']!r}, expected {args.config!r}")
        if mismatches:
            print(f"  {case['case_id']}: FAIL — {'; '.join(mismatches)}")
            contract_failures += 1
            grouped[case["complexity"]].append(False)
            continue
        ok = _record_passed(case, record)
        passed += int(ok)
        grouped[case["complexity"]].append(ok)
        print(f"  {case['case_id']}: {'PASS' if ok else 'FAIL'}")

    _print_groups(grouped, args.group_by)
    rate = passed / len(cases)
    print(f"pass rate: {passed}/{len(cases)} = {rate:.3f}; required {args.fail_under:.3f}")
    if contract_failures:
        print(f"contract failures: {contract_failures} (always fatal)")
    return 0 if not contract_failures and rate >= args.fail_under else 1


def _print_groups(grouped: dict[str, list[bool]], group_by: str | None) -> None:
    if group_by != "complexity":
        return
    print("by complexity:")
    for name, values in sorted(grouped.items()):
        print(f"  {name}: {sum(values)}/{len(values)}")


def _settings(args: argparse.Namespace) -> EvaluationSettings:
    return EvaluationSettings(
        cases_dir=args.cases,
        manifest_path=args.manifest,
        flagd_config=args.flagd_config,
        prometheus_url=args.prometheus_url,
        api_url=args.api_url,
        artifacts_dir=args.artifacts,
        lock_file=args.lock_file,
        fail_under=args.fail_under,
        group_by=args.group_by,
        run_timeout=args.run_timeout,
        api_poll_interval=args.api_poll_interval,
        signal_timeout=args.signal_timeout,
        signal_interval=args.signal_interval,
        signal_successes=args.signal_successes,
    )


# Historical CLI output is the live/preflight result lines. The extra phases
# exist for the Control Center and must not change `make eval-live-dev`.
_CLI_PHASES = frozenset({"injecting", "scoring", "complete"})


def _print_event(event: EvaluationEvent) -> None:
    if event.phase in _CLI_PHASES and event.message:
        print(event.message)


def run_live(args: argparse.Namespace) -> int:
    if not args.yes_inject_faults:
        print("live mode refused: pass --yes-inject-faults to acknowledge lab mutation")
        return 2
    try:
        result = execute_evaluation(
            EvaluationRequest(kind="deterministic", config_id=args.config),
            _settings(args),
            _print_event,
        )
    except ValueError as exc:
        print(f"live FAIL: {exc}")
        return 1
    except EvaluationRefused as exc:
        print(f"live mode refused before fault injection: {exc}")
        return 2
    except (LiveEvaluationLocked, OSError) as exc:
        print(f"live mode lock unavailable: {exc}")
        return 2
    return 0 if result.passed else 1


def run_preflight(args: argparse.Namespace) -> int:
    """Check the complete C1 boundary without injecting or executing a fault."""
    try:
        execute_evaluation(
            EvaluationRequest(kind="preflight", config_id=args.config),
            _settings(args),
            _print_event,
        )
    except ValueError as exc:
        print(f"preflight FAIL: {exc}")
        return 1
    except (
        FaultInjectionError,
        IncidentApiError,
        LiveEvaluationLocked,
        OSError,
        SignalReadError,
    ) as exc:
        print(f"preflight FAIL: {exc}")
        return 2
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run On-Call Copilot evaluations.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true", help="replay committed development fixtures")
    mode.add_argument(
        "--preflight", action="store_true", help="check live dependencies without injecting a fault"
    )
    mode.add_argument("--live", action="store_true", help="inject faults and call the incident API")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path("eval/faults/manifest.yaml"))
    parser.add_argument("--fixtures", type=Path, default=Path("eval/fixtures"))
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path(os.environ.get("ONCALL_EVAL_RUNS", "artifacts/runs")),
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=Path(
            os.environ.get("ONCALL_EVAL_LOCK_FILE", "artifacts/locks/live-evaluation.lock")
        ),
    )
    parser.add_argument("--fail-under", type=float, required=True)
    parser.add_argument(
        "--config", choices=("c0_workflow", "c1_single", "c2_team"), default="c1_single"
    )
    parser.add_argument("--group-by", choices=("complexity",), default=None)
    parser.add_argument("--allow-heldout", action="store_true")
    parser.add_argument("--yes-inject-faults", action="store_true")
    parser.add_argument(
        "--api-url", default=os.environ.get("ONCALL_SERVICE_URL", "http://localhost:8000")
    )
    parser.add_argument(
        "--prometheus-url",
        default=os.environ.get("PROMETHEUS_URL", "http://10.241.2.69:9090"),
    )
    parser.add_argument(
        "--flagd-config",
        type=Path,
        default=Path(os.environ.get("FLAGD_CONFIG", "deploy/flagd/demo.flagd.json")),
    )
    parser.add_argument("--run-timeout", type=float, default=600.0)
    parser.add_argument("--api-poll-interval", type=float, default=1.0)
    parser.add_argument("--signal-timeout", type=float, default=120.0)
    parser.add_argument("--signal-interval", type=float, default=5.0)
    parser.add_argument("--signal-successes", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not 0 <= args.fail_under <= 1:
        parser.error("--fail-under must be between 0 and 1")
    if _is_heldout(args.cases) and not args.allow_heldout:
        parser.error("held-out cases require the explicit --allow-heldout acknowledgement")
    if args.mock:
        return run_mock(args)
    if args.preflight:
        return run_preflight(args)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
