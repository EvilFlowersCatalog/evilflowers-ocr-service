"""One `files.ocr` run, end to end: validate the kwargs, OCR, write the outcome file, decide the task's fate.

Order matters (issue #5, EvilFlowersCatalog#169):

1. ``destination`` is written first (``processing``), for a ``done`` run only;
2. the outcome file is written last, for every status. It is the commit marker: the
   catalog applies the run when it reads the file on ``task-succeeded`` /
   ``task-failed``, or on its reconcile tick if the event was lost;
3. a ``failed`` outcome is written and then raised, so the job carries both the
   domain error and the traceback.

Never written: an outcome for a run that was interrupted (SIGTERM, cold shutdown,
``SystemExit`` from ``ocr_hooks``). The message goes back to Redis and the
redelivery, carrying the same token, writes it. A ``failed`` file would consume
the token and make the catalog refuse the redelivery's outcome.

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
from pydantic import ValidationError

from evilflowers_ocr_worker import outcome as outcome_file
from evilflowers_ocr_worker import processing
from evilflowers_ocr_worker.contract import OcrRequest
from evilflowers_ocr_worker.observability import metrics
from evilflowers_ocr_worker.observability.context import bind
from evilflowers_ocr_worker.observability.otel import SCOPE, Status, StatusCode, trace
from evilflowers_ocr_worker.processing import Outcome
from evilflowers_ocr_worker.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class InvalidRequest(ValueError):
    """The task's kwargs do not match the contract."""


class OcrFailed(RuntimeError):
    """The OCR outcome is `failed`; raised after the outcome file is written so the Celery task fails."""


def run(task_id: str | None, kwargs: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    try:
        request = OcrRequest.model_validate(kwargs)
    except ValidationError as error:
        raise InvalidRequest(f"task kwargs do not match the files.ocr contract: {error}") from error
    # Before any OCR: a run whose outcome has nowhere to go would only burn CPU.
    outcome_path = _outcome_path(request, settings)

    run_id = request.run_id or task_id or request.job_id
    if request.run_id and task_id and request.run_id != task_id:
        logger.warning("run_id %s differs from the task id %s; using run_id", request.run_id, task_id)

    with (
        _span("ocr.run") as span,
        bind(
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
        outcome_file.write_outcome(outcome_path, outcome.file(run_id, request.token))
        metrics.record_outcome(outcome.status)

        if outcome.status == "failed":
            logger.error("OCR failed: %s (outcome: %s)", outcome.error, request.outcome)
            raise OcrFailed(outcome.error or "OCR failed") from outcome.exception
        logger.info("OCR %s (outcome: %s)", outcome.status, request.outcome)
        return outcome.result()


def _outcome_path(request: OcrRequest, settings: Settings) -> Path:
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
    # Exception only: SystemExit (an interrupted run) must propagate and never become a `failed` outcome.
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
    if outcome.status == "failed" and Status is not None:
        span.set_status(Status(StatusCode.ERROR, outcome.error or "failed"))


class _NoSpan:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _span(name: str):
    if trace is None:
        return _NoSpan()
    # The span is ended on exit; exceptions are recorded by the Celery instrumentation's consumer span.
    return trace.get_tracer(SCOPE).start_as_current_span(name, record_exception=False, set_status_on_exception=False)
