"""Process-wide exclusion for evaluations that mutate the shared lab."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO


class LiveEvaluationLocked(RuntimeError):
    """Another process owns the shared live-evaluation critical section."""


def _owner(handle: IO[str]) -> str:
    handle.seek(0)
    value = handle.read().strip()
    return value or "owner metadata unavailable"


def live_evaluation_lock_held(path: Path) -> bool:
    """Return whether some open file description already holds ``path``.

    A failed non-blocking lock is released by closing the probe descriptor.
    This function never writes or truncates the file, and it does not create
    a missing one. Same-process exclusion for the Control Center is still the
    job store: this probe is how that process notices a CLI run.
    """
    if not path.is_file():
        return False
    try:
        handle = path.open("a+", encoding="utf-8")
    except OSError:
        return False
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


@contextmanager
def live_evaluation_lock(path: Path) -> Iterator[None]:
    """Hold a non-blocking advisory lock without unlinking its inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LiveEvaluationLocked(
                f"another live evaluation holds {path}: {_owner(handle)}"
            ) from exc

        metadata = {
            "pid": os.getpid(),
            "acquired_at": datetime.now(UTC).isoformat(),
        }
        handle.seek(0)
        handle.truncate()
        json.dump(metadata, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        try:
            yield
        finally:
            # Keep the inode: unlinking a locked file lets a second process lock
            # a new inode at the same path while the first lock is still held.
            handle.seek(0)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
