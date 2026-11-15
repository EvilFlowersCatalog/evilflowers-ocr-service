"""One `files.ocr` run, end to end: OCR, write the outcome file, decide the task's fate.

``tasks`` calls ``run`` through the kit's ``runtime.run_task``, which has already
validated the kwargs (``OcrRequest``) and counted the delivery. Past
``MAX_DELIVERIES`` it calls ``give_up`` instead.

Order matters (issue #5, EvilFlowersCatalog#169):

1. ``destination`` is written first (``processing``), for a ``done`` run only;
2. the outcome file is written last, for every status (the kit's ``write_outcome``).
   It is the commit marker: the catalog applies the run when it reads the file on
   ``task-succeeded`` / ``task-failed``, or on its reconcile tick if the event was lost;
3. a ``failed`` outcome is written and then raised, so the job carries both the
   domain error and the traceback.

Never written: an outcome for a run that was interrupted (SIGTERM past the grace,
the kit's cold cancel: billiard's ``SystemExit`` from ``ocr_hooks``). The message goes
back to Redis and the redelivery, carrying the same token, writes it. A ``failed``
file would consume the token and make the catalog refuse the redelivery's outcome.

The worker never deletes what it wrote: when a run was revoked or replaced, the
catalog deletes ``destination`` and the outcome file itself.

The return value is compact (status, pages, engine, seconds): Celery puts a repr of
it, capped at 1024 characters, into the event. The full outcome lives in the file.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from evilflowers_worker import observability
from evilflowers_worker.outcome import write_outcome

from evilflowers_ocr_worker import metrics, processing
from evilflowers_ocr_worker.contract import OcrRequest
from evilflowers_ocr_worker.processing import Outcome
from evilflowers_ocr_worker.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class InvalidRequest(ValueError):
    """The task's kwargs do not match the contract."""


class OcrFailed(RuntimeError):
    """The OCR outcome is `failed`; raised after the outcome file is written so the Celery task fails."""


class GaveUp(RuntimeError):
    """Past MAX_DELIVERIES: the book killed its worker on every delivery; raised after the `failed` outcome."""


def run(request: OcrRequest, task_id: str | None, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    # Before any OCR: a run whose outcome has nowhere to go would only burn CPU.
    _check_outcome_path(request, settings)
    run_id = _run_id(request, task_id)

    with (
        observability.span("ocr.run") as span,
        observability.bind(
            job_id=request.job_id,
            run_id=run_id,
            acquisition_id=request.acquisition_id,
            engine=request.engine or settings.default_engine,
            language=request.language,
        ),
    ):
        logger.info("OCR started: %s -> %s", request.source, request.destination)
        outcome = _process(request, settings, run_id)
        _annotate(span, outcome)
        metrics.record_run(
            outcome.engine or "unknown", outcome.status, outcome.seconds, outcome.pages, outcome.pages_ocred
        )
        write_outcome(settings, request.outcome, outcome.file(run_id, request.token))
        metrics.record_outcome(outcome.status)

        if outcome.status == "failed":
            logger.error("OCR failed: %s (outcome: %s)", outcome.error, request.outcome)
            raise OcrFailed(outcome.error or "OCR failed") from outcome.exception
        logger.info("OCR %s (outcome: %s)", outcome.status, request.outcome)
        return outcome.result()


def give_up(request: OcrRequest, task_id: str | None, delivery: int, settings: Settings | None = None) -> None:
    """Past the delivery cap: write a `failed` outcome (the catalog ends the job `failed`, not `lost`) and raise."""
    settings = settings or get_settings()
    _check_outcome_path(request, settings)
    run_id = _run_id(request, task_id)
    error = f"gave up after {delivery - 1} deliveries: the worker died while OCRing this PDF every time"
    outcome = Outcome("failed", error=error, engine=request.engine or settings.default_engine)
    with observability.bind(job_id=request.job_id, run_id=run_id, acquisition_id=request.acquisition_id):
        write_outcome(settings, request.outcome, outcome.file(run_id, request.token))
        metrics.record_outcome("failed")
        logger.error("OCR %s (outcome: %s)", error, request.outcome)
        raise GaveUp(error)


def _run_id(request: OcrRequest, task_id: str | None) -> str:
    if request.run_id and task_id and request.run_id != task_id:
        logger.warning("run_id %s differs from the task id %s; using run_id", request.run_id, task_id)
    return request.run_id or task_id or request.job_id


def _check_outcome_path(request: OcrRequest, settings: Settings) -> Path:
    try:
        path = settings.storage_file(request.outcome)
    except ValueError as error:
        raise InvalidRequest(f"outcome: {error}") from error
    for name in ("source", "destination"):
        try:
            other = settings.storage_file(getattr(request, name))
        except ValueError:
            continue  # processing reports a bad source / destination as a `failed` outcome
        if path == other:
            raise InvalidRequest(f"outcome equals {name}: {request.outcome!r}")
    return path


def _process(request: OcrRequest, settings: Settings, run_id: str) -> Outcome:
    engine = request.engine or settings.default_engine
    # The run's own deadline (see ocr_hooks): stops ocrmypdf between pages, leaving time to write
    # the outcome before Celery's hard limit kills the process.
    deadline = time.monotonic() + settings.task_soft_time_limit
    try:
        return processing.run(request, settings, run_id, deadline=deadline)
    except SoftTimeLimitExceeded as error:
        message = f"time limit exceeded ({settings.task_soft_time_limit} s)"
        return Outcome("failed", error=message, engine=engine, exception=error)
    # Exception only: SystemExit (an interrupted run, the kit's cold cancel) must propagate and never become a
    # `failed` outcome.
    except Exception as error:
        logger.exception("OCR crashed")
        return Outcome("failed", error=f"{type(error).__name__}: {error}", engine=engine, exception=error)


def _annotate(span, outcome: Outcome) -> None:
    if span is None:
        return
    attributes = {
        "ocr.outcome": outcome.status,
        "ocr.engine": outcome.engine,
        "ocr.engine.version": outcome.engine_version,
        "ocr.pages": outcome.pages,
        "ocr.pages_ocred": outcome.pages_ocred,
        "ocr.skipped_reason": outcome.skipped_reason,
    }
    span.set_attributes({key: value for key, value in attributes.items() if value is not None})
    if outcome.status == "failed":
        observability.fail(outcome.exception or RuntimeError(outcome.error or "failed"))
