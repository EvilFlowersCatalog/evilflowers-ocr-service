"""Telemetry and JSON logs wired into the Celery worker's signals.

``setup()`` runs on ``worker_process_init``, which fires in the process that runs
the tasks: each prefork child after the fork (exporter threads do not survive a
fork), as in the analyzer, image and equation workers.
"""

import time

from celery import signals

from evilflowers_ocr_worker.observability import metrics
from evilflowers_ocr_worker.observability.logs import configure_logger
from evilflowers_ocr_worker.observability.setup import setup, shutdown

_task_started: dict[str, float] = {}


def _task_prerun(task_id: str | None = None, **_kwargs) -> None:
    if task_id:
        _task_started[task_id] = time.perf_counter()


def _task_postrun(task_id: str | None = None, task=None, state: str | None = None, **_kwargs) -> None:
    started = _task_started.pop(task_id, None)
    if started is not None and task is not None:
        metrics.TASK_DURATION.record(
            time.perf_counter() - started, {"celery.task.name": task.name, "celery.task.state": state or "UNKNOWN"}
        )


def connect() -> None:
    """Hook telemetry and logs into the worker; call once, where the app is defined."""
    signals.worker_process_init.connect(lambda **_: setup(), weak=False)
    signals.worker_process_shutdown.connect(shutdown, weak=False)
    signals.worker_shutdown.connect(shutdown, weak=False)
    signals.after_setup_logger.connect(configure_logger, weak=False)
    signals.after_setup_task_logger.connect(configure_logger, weak=False)
    signals.task_prerun.connect(_task_prerun, weak=False)
    signals.task_postrun.connect(_task_postrun, weak=False)
