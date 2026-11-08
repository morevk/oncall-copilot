"""Versioned, separate rubric artifacts for human and LLM judges."""

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

RUBRIC_DIMENSIONS = (
    "evidence_grounding",
    "hypothesis_quality",
    "tool_selection",
    "uncertainty",
    "explanation_quality",
    "action_justification",
    "recovery_explanation",
)


class JudgeMetadata(TypedDict):
    prompt_sha256: str
    evidence_sha256: str
    calibration_status: Literal["exploratory", "calibrated"]
    latency_ms: int
    input_tokens: NotRequired[int]
    output_tokens: NotRequired[int]
    cost_usd: NotRequired[float]


class RubricRecord(TypedDict):
    run_id: str
    rubric_version: str
    scorer_type: Literal["human", "llm"]
    scorer_id: str
    dimensions: dict[str, int]
    explanations: NotRequired[dict[str, str]]
    evidence_refs: NotRequired[dict[str, list[str]]]
    judge_metadata: NotRequired[JudgeMetadata]


def validate_rubric_record(value: object) -> list[str]:
    if not isinstance(value, dict):
        return ["rubric record must be an object"]
    problems: list[str] = []
    allowed = set(RubricRecord.__annotations__)
    required = set(RubricRecord.__required_keys__)
    if missing := required - set(value):
        problems.append(f"missing fields {sorted(missing)}")
    if unknown := set(value) - allowed:
        problems.append(f"unknown fields {sorted(unknown)}")
    for field in ("run_id", "rubric_version", "scorer_id"):
        if not isinstance(value.get(field), str) or not value.get(field):
            problems.append(f"{field} must be a non-empty string")
    if value.get("scorer_type") not in {"human", "llm"}:
        problems.append("scorer_type must be 'human' or 'llm'")

    dimensions = value.get("dimensions")
    if not isinstance(dimensions, dict):
        problems.append("dimensions must be an object")
    else:
        if set(dimensions) != set(RUBRIC_DIMENSIONS):
            problems.append(f"dimensions must contain exactly {list(RUBRIC_DIMENSIONS)}")
        for dimension, score in dimensions.items():
            if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 5:
                problems.append(f"dimensions.{dimension} must be an integer from 0 to 5")

    explanations = value.get("explanations", {})
    if not isinstance(explanations, dict) or any(
        key not in RUBRIC_DIMENSIONS or not isinstance(text, str)
        for key, text in explanations.items()
    ):
        problems.append("explanations must map rubric dimensions to strings")
    refs = value.get("evidence_refs", {})
    if not isinstance(refs, dict) or any(
        key not in RUBRIC_DIMENSIONS
        or not isinstance(items, list)
        or any(not isinstance(item, str) for item in items)
        for key, items in refs.items()
    ):
        problems.append("evidence_refs must map rubric dimensions to lists of strings")
    metadata = value.get("judge_metadata")
    if metadata is not None:
        problems.extend(_validate_judge_metadata(metadata))
    return problems


def _validate_judge_metadata(value: object) -> list[str]:
    if not isinstance(value, dict):
        return ["judge_metadata must be an object"]
    allowed = set(JudgeMetadata.__annotations__)
    required = set(JudgeMetadata.__required_keys__)
    problems = []
    if missing := required - set(value):
        problems.append(f"judge_metadata missing fields {sorted(missing)}")
    if unknown := set(value) - allowed:
        problems.append(f"judge_metadata has unknown fields {sorted(unknown)}")
    for field in ("prompt_sha256", "evidence_sha256"):
        item = value.get(field)
        if (
            not isinstance(item, str)
            or len(item) != 64
            or any(character not in "0123456789abcdef" for character in item)
        ):
            problems.append(f"judge_metadata.{field} must be a SHA-256 hex digest")
    if value.get("calibration_status") not in {"exploratory", "calibrated"}:
        problems.append("judge_metadata.calibration_status must be exploratory or calibrated")
    for field in ("latency_ms", "input_tokens", "output_tokens"):
        if field not in value:
            continue
        item = value[field]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            problems.append(f"judge_metadata.{field} must be a non-negative integer")
    if "cost_usd" in value:
        cost = value["cost_usd"]
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(cost)
            or cost < 0
        ):
            problems.append("judge_metadata.cost_usd must be a non-negative number")
    return problems


def load_rubric_record(path: Path) -> RubricRecord:
    value = json.loads(path.read_text(encoding="utf-8"))
    if problems := validate_rubric_record(value):
        raise ValueError("invalid rubric record: " + "; ".join(problems))
    return value  # type: ignore[return-value]


def write_rubric_record(path: Path, record: RubricRecord) -> None:
    """Write once and atomically; rubric history is evidence, not mutable UI state."""
    if problems := validate_rubric_record(record):
        raise ValueError("invalid rubric record: " + "; ".join(problems))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"rubric artifact already exists: {path}")
    raw = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
