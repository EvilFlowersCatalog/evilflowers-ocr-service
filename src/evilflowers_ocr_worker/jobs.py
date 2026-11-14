"""One `files.ocr` run, end to end: validate the kwargs, OCR, report, decide the task's fate.

Order matters (issue #3): the report goes out before the task returns, so the
catalog usually sees the report before ``task-succeeded``. A ``failed`` outcome is
reported too and then raised, so the job carries both the domain error and the
traceback. The task's return value is the report plus the engine and timing; it
becomes the job's ``result`` in the ledger.

The destination is deleted on 409 only (issue #3): the catalog said the job was
cancelled or a newer run replaced this one, so nothing will ever point at the file.
Every other non-200 keeps it. After a lost response (the catalog applied the POST
but the answer timed out) followed by 5xx, the acquisition may already point at
the file, and deleting it would break a published book. An orphan on the shared
volume is the cheaper failure.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from pydantic import ValidationError

from evilflowers_ocr_worker import processing, report
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
    """The OCR outcome is `failed`; raised after the report so the Celery task fails."""


def run(task_id: str | None, kwargs: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    try:
        request = OcrRequest.model_validate(kwargs)
    except ValidationError as error:
        raise InvalidRequest(f"task kwargs do not match the files.ocr contract: {error}") from error

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
        result = outcome.result()
        result["report"] = _deliver(request, outcome, settings)

        if outcome.status == "failed":
            logger.error("OCR failed: %s", outcome.error)
            raise OcrFailed(outcome.error or "OCR failed") from outcome.exception
        logger.info("OCR %s (report: %s)", outcome.status, result["report"])
        return result


def _process(request: OcrRequest, settings: Settings, run_id: str) -> Outcome:
    engine = request.engine or settings.default_engine
    # The run's own deadline (see ocr_hooks): stops ocrmypdf between pages, leaving time to report
    # before Celery's hard limit kills the process.
    deadline = time.monotonic() + settings.task_soft_time_limit
    try:
        return processing.run(request, settings, run_id, deadline=deadline)
    except SoftTimeLimitExceeded as error:
        message = f"time limit exceeded ({settings.task_soft_time_limit} s)"
        return Outcome("failed", error=message, engine=engine, exception=error)
    except Exception as error:
        logger.exception("OCR crashed")
        return Outcome("failed", error=f"{type(error).__name__}: {error}", engine=engine, exception=error)


def _deliver(request: OcrRequest, outcome: Outcome, settings: Settings) -> str:
    if not request.report_url:
        logger.error("No report_url in the task; the catalog will not apply this run")
        return "not_sent"
    try:
        delivery = report.send(request.report_url, outcome.report().body(), settings)
    except (report.ReportRejected, report.CatalogUnavailable):
        if outcome.output:
            logger.error("Report not accepted; %s stays on the volume for an operator", outcome.output)
        raise
    if delivery is report.Delivery.CONFLICT:
        _discard(outcome, settings, "the job was cancelled or replaced by a newer run (409)")
    return delivery.value


def _discard(outcome: Outcome, settings: Settings, why: str) -> None:
    if outcome.output and processing.remove_output(settings, outcome.output):
        logger.warning("Deleted %s: %s", outcome.output, why)


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
