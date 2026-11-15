"""Atomic writes onto the shared volume, and the outcome file (issue #5).

``atomic_write`` puts a file in place under a temporary name, fsyncs it, renames it
over the target and fsyncs the directory, so the target either does not exist or is
complete. ``processing`` publishes ``destination`` with it; ``write_outcome`` writes
the outcome file with it.

The outcome file is the run's commit marker: it is written last, after
``destination``, so a crash in between leaves no outcome and the catalog ends the
run ``lost`` instead of applying a half-finished one. An interrupted run (SIGTERM,
cold shutdown) writes none either: the message goes back to Redis and the
redelivery, carrying the same token, writes it.

This is the shape evilflowers-worker's ``outcome`` module takes (evilflowers-worker#3);
issue #6 swaps it for the kit's.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path

from evilflowers_ocr_worker.contract import OutcomeFile

PARTIAL_SUFFIX = ".partial"


def atomic_write(path: Path, data: bytes | Path, *, partial: Path | None = None) -> None:
    """Write ``data`` (bytes, or the contents of a file) to ``path``: temporary name, fsync, rename, fsync dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = partial or path.with_name(f".{path.name}{PARTIAL_SUFFIX}")
    try:
        with partial.open("wb") as writer:
            if isinstance(data, Path):
                with data.open("rb") as reader:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
            else:
                writer.write(data)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(partial, path)
    except BaseException:
        remove(partial)
        raise
    fsync_directory(path.parent)


def write_outcome(path: Path, outcome: OutcomeFile) -> None:
    """Write the outcome file at ``path`` (absolute, already checked against STORAGE_PATH)."""
    atomic_write(path, outcome.model_dump_json(indent=2).encode())


def fsync_directory(directory: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def remove(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
