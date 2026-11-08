"""Command-line entry point for one completed-run LLM judgement."""

from __future__ import annotations

import argparse
from pathlib import Path

from oncall_eval.judge.core import (
    fetch_incident,
    judge_run,
    settings_from_environment,
)
from oncall_eval.runner import load_run_record


def main() -> int:
    parser = argparse.ArgumentParser(description="Score one completed incident with the LLM judge")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument("--rubric", type=Path, default=Path("eval/rubrics/investigation-v1.yaml"))
    parser.add_argument("--api-url")
    args = parser.parse_args()

    run_path = args.artifacts / "runs" / f"{args.run_id}.json"
    run = load_run_record(run_path)
    settings = settings_from_environment(
        rubric_path=args.rubric,
        artifacts_dir=args.artifacts,
        incident_api_url=args.api_url,
    )
    incident_id = run.get("source_incident_id") or f"eval-{run['run_id']}"
    incident = fetch_incident(settings.incident_api_url, incident_id, settings.timeout_seconds)
    result = judge_run(run, incident, settings)
    total = sum(result.record["dimensions"].values())
    print(
        f"LLM rubric {result.record['rubric_version']}: {total}/35 "
        f"({result.record['judge_metadata']['calibration_status']})"
    )
    print(result.artifact_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
