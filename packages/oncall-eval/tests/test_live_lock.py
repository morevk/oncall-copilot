import json
from pathlib import Path

import pytest
from oncall_eval.live_lock import (
    LiveEvaluationLocked,
    live_evaluation_lock,
    live_evaluation_lock_held,
)


def test_live_evaluation_lock_records_owner_and_refuses_a_second_runner(tmp_path: Path):
    path = tmp_path / "locks" / "live-evaluation.lock"

    with live_evaluation_lock(path):
        owner = json.loads(path.read_text(encoding="utf-8"))
        assert owner["pid"] > 0
        assert owner["acquired_at"]

        with (
            pytest.raises(LiveEvaluationLocked, match="another live evaluation"),
            live_evaluation_lock(path),
        ):
            raise AssertionError("the second runner must never enter")

    assert path.is_file()
    assert path.read_text(encoding="utf-8") == ""


def test_lock_probe_sees_a_holder_and_does_not_create_or_clear_the_file(tmp_path: Path):
    path = tmp_path / "locks" / "live-evaluation.lock"

    assert live_evaluation_lock_held(path) is False
    assert not path.exists()

    with live_evaluation_lock(path):
        owned = path.read_text(encoding="utf-8")
        assert live_evaluation_lock_held(path) is True
        assert path.read_text(encoding="utf-8") == owned
        with pytest.raises(LiveEvaluationLocked), live_evaluation_lock(path):
            raise AssertionError("the probe must not have released the holder")

    assert live_evaluation_lock_held(path) is False
    assert path.read_text(encoding="utf-8") == ""


def test_live_evaluation_lock_releases_after_an_exception(tmp_path: Path):
    path = tmp_path / "live.lock"

    with pytest.raises(RuntimeError, match="evaluation failed"), live_evaluation_lock(path):
        raise RuntimeError("evaluation failed")

    with live_evaluation_lock(path):
        assert json.loads(path.read_text(encoding="utf-8"))["pid"] > 0
