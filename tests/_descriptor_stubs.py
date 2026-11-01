"""``os.fstat`` stand-ins that answer only the descriptors a test owns.

The descriptor-hardening suites (``test_task_status_coverage.py``,
``test_storage_and_file_boundary_behaviors.py``) stage identity races and
special-file swaps by replacing ``os.fstat`` through ``monkeypatch``. The
replacement is process-wide, and pytest's fd capture reads
``os.fstat(<capture fd>).st_size`` on its own descriptors while a test runs
(``FDCapture.snap``, pytest 9.2+), so a stub that answers every descriptor
breaks the runner: a bare ``SimpleNamespace`` has no ``st_size`` and a
raising stub fails the capture read itself. These helpers scope a stub to the
descriptors the test fabricated, or to the file whose identity it is racing,
and defer every other descriptor to the real call.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Collection, Iterable
from pathlib import Path
from typing import Any

_REAL_FSTAT = os.fstat


def fstat_answering(fds: Collection[int], answer: Callable[[int], Any]) -> Callable[[int], Any]:
    """Return an ``os.fstat`` stand-in that calls ``answer`` for ``fds`` only.

    Meant for fabricated descriptor numbers a test hands to the code under
    test (``os.open`` stubbed to return ``701``); any other descriptor, the
    runner's included, gets the real ``os.fstat`` result.
    """

    def fstat(fd: int) -> Any:
        if fd in fds:
            return answer(fd)
        return _REAL_FSTAT(fd)

    return fstat


def fstat_altering(
    targets: Iterable[Path], alter: Callable[[os.stat_result], Any]
) -> Callable[[int], Any]:
    """Return an ``os.fstat`` stand-in that rewrites the result for ``targets``.

    A descriptor is matched to a target by device and inode at call time, so a
    file the code under test creates during the call (``O_CREAT``) is matched
    too; ``alter`` receives the real metadata and returns what the test wants
    the caller to see. Every other descriptor gets the real result.
    """
    paths = tuple(targets)

    def fstat(fd: int) -> Any:
        metadata = _REAL_FSTAT(fd)
        for path in paths:
            try:
                identity = os.lstat(path)
            except FileNotFoundError:
                continue
            if (identity.st_dev, identity.st_ino) == (metadata.st_dev, metadata.st_ino):
                return alter(metadata)
        return metadata

    return fstat
