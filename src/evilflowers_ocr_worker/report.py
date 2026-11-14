"""Report the outcome to the catalog (issue #3): ``POST {report_url}``.

The report is what changes the catalog; Celery events only move the job's status.

======  ==============================================  ==========================================
Status  Meaning                                         Worker
======  ==============================================  ==========================================
200     accepted (idempotent: the first report wins)    done
400     contract error (wrong output, file missing…)    fail the task, no retry
401/403 key missing or lacking `tasks.report_job`       fail the task (deployment problem)
404     job / run unknown                               log and finish
409     job cancelled, or a newer run replaced this one finish; the caller deletes the output
5xx     catalog down (also a network error)             retry with backoff, then fail the task
======  ==============================================  ==========================================
"""

from __future__ import annotations

import enum
import logging
import time
from collections.abc import Callable
from typing import Any

import requests

from evilflowers_ocr_worker import __version__
from evilflowers_ocr_worker.observability import metrics
from evilflowers_ocr_worker.observability.otel import propagate
from evilflowers_ocr_worker.settings import Settings

logger = logging.getLogger(__name__)

USER_AGENT = f"evilflowers-ocr-service/{__version__}"


class Delivery(enum.Enum):
    ACCEPTED = "accepted"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"


class ReportRejected(Exception):
    """400/401/403 (or another 4xx): retrying cannot help; the task fails."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"catalog rejected the report: HTTP {status}: {detail}")
        self.status = status


class CatalogUnavailable(Exception):
    """5xx or network errors on every attempt."""


def session(settings: Settings) -> requests.Session:
    http = requests.Session()
    http.headers["User-Agent"] = USER_AGENT
    if settings.catalog_api_key is not None and (secret := settings.catalog_api_key.get_secret_value()):
        http.headers["Authorization"] = f"Bearer {secret}"
    return http


def send(
    url: str,
    body: dict[str, Any],
    settings: Settings,
    *,
    http: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Delivery:
    http = http or session(settings)
    last_error = ""
    for attempt in range(1, settings.report_attempts + 1):
        headers = {"Content-Type": "application/json"}
        # `traceparent`: the catalog's request span joins this task's trace. The requests
        # instrumentation (when telemetry is on) replaces it with its own client span's context.
        if propagate is not None:
            propagate.inject(headers)
        try:
            response = http.post(url, json=body, headers=headers, timeout=settings.catalog_timeout)
        except requests.RequestException as error:
            last_error = f"{type(error).__name__}: {error}"
            metrics.record_report("error")
        else:
            status = response.status_code
            metrics.record_report(str(status))
            if 200 <= status < 300:
                return Delivery.ACCEPTED
            if status == 404:
                logger.warning("Catalog does not know this run (404); finishing: %s", _detail(response))
                return Delivery.NOT_FOUND
            if status == 409:
                logger.warning("Job cancelled or replaced by a newer run (409): %s", _detail(response))
                return Delivery.CONFLICT
            if status < 500:
                level = logging.CRITICAL if status in (401, 403) else logging.ERROR
                logger.log(level, "Catalog rejected the report (HTTP %d): %s", status, _detail(response))
                raise ReportRejected(status, _detail(response))
            last_error = f"HTTP {status}: {_detail(response)}"

        if attempt < settings.report_attempts:
            delay = _delay(settings, attempt)
            logger.warning(
                "Report attempt %d/%d failed (%s); retrying in %.0f s",
                attempt,
                settings.report_attempts,
                last_error,
                delay,
            )
            sleep(delay)
    raise CatalogUnavailable(f"report not delivered after {settings.report_attempts} attempts: {last_error}")


def _delay(settings: Settings, attempt: int) -> float:
    backoff = settings.report_backoff or (0.0,)
    return backoff[min(attempt - 1, len(backoff) - 1)]


def _detail(response: requests.Response) -> str:
    text = (response.text or "").strip()
    return text[:500] if text else response.reason or ""
