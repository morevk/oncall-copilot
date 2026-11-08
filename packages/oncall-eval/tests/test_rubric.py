import json
from pathlib import Path

import pytest
from oncall_eval.rubric import (
    RUBRIC_DIMENSIONS,
    RubricRecord,
    load_rubric_record,
    validate_rubric_record,
    write_rubric_record,
)


def _record() -> RubricRecord:
    return {
        "run_id": "run-001",
        "rubric_version": "investigation-v1",
        "scorer_type": "llm",
        "scorer_id": "judge/model",
        "dimensions": {dimension: 4 for dimension in RUBRIC_DIMENSIONS},
        "explanations": {"evidence_grounding": "Claims cite collected evidence."},
        "evidence_refs": {"evidence_grounding": ["event:evidence-2"]},
    }


def test_rubric_round_trips_and_is_write_once(tmp_path: Path):
    path = tmp_path / "rubrics" / "run-001.json"
    write_rubric_record(path, _record())

    assert load_rubric_record(path) == _record()
    with pytest.raises(FileExistsError):
        write_rubric_record(path, _record())


def test_rubric_requires_all_seven_zero_to_five_dimensions():
    record = _record()
    record["dimensions"].pop("uncertainty")
    record["dimensions"]["tool_selection"] = 6

    problems = validate_rubric_record(record)

    assert any("dimensions must contain exactly" in problem for problem in problems)
    assert "dimensions.tool_selection must be an integer from 0 to 5" in problems


def test_rubric_loader_rejects_unknown_fields(tmp_path: Path):
    value = dict(_record())
    value["total"] = 28
    path = tmp_path / "rubric.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown fields"):
        load_rubric_record(path)
