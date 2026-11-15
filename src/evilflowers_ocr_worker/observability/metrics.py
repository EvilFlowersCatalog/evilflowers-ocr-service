"""The worker's metrics, declared once; each is created on first use and is a no-op without OpenTelemetry.

Exported over OTLP and stored in Mimir under the Prometheus names of issue #4:

=====================================  ==========================================================
Prometheus name                        Instrument
=====================================  ==========================================================
ocr_pages_total{outcome}               ``ocr.pages`` counter, pages by outcome (``ocred``, ``skipped``)
ocr_duration_seconds                   ``ocr.duration`` histogram, one OCR run, by engine and outcome
ocr_job_outcome_total{job,outcome}     ``ocr.job.outcome`` counter, outcome files written, by status.
                                       The evilflowers-worker kit's standard name (issue #6)
=====================================  ==========================================================

Attributes stay low-cardinality: never a job or acquisition id (those go on spans and log lines).
"""

import threading
from dataclasses import dataclass, field
from typing import Any, Literal

from evilflowers_ocr_worker.observability.otel import SCOPE, metrics

# Seconds. One page (~2 s) up to a 1,000-page book.
DURATION_BUCKETS = (1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 10800, 14400)

_lock = threading.Lock()


@dataclass(eq=False)
class Instrument:
    kind: Literal["counter", "histogram"]
    name: str
    unit: str
    description: str
    buckets: tuple[float, ...] | None = None
    _created: Any = field(default=None, init=False, repr=False)

    def _get(self):
        if metrics is None:
            return None
        if self._created is None:
            with _lock:
                if self._created is None:
                    # A proxy meter: its instruments become real once `setup()` sets the provider.
                    meter = metrics.get_meter(SCOPE)
                    if self.kind == "counter":
                        self._created = meter.create_counter(self.name, self.unit, self.description)
                    else:
                        self._created = meter.create_histogram(
                            self.name,
                            self.unit,
                            self.description,
                            explicit_bucket_boundaries_advisory=list(self.buckets or DURATION_BUCKETS),
                        )
        return self._created

    def add(self, value: float = 1, attributes: dict[str, Any] | None = None) -> None:
        if (instrument := self._get()) is not None:
            instrument.add(value, attributes or {})

    def record(self, value: float, attributes: dict[str, Any] | None = None) -> None:
        if (instrument := self._get()) is not None:
            instrument.record(value, attributes or {})


PAGES = Instrument("counter", "ocr.pages", "{page}", "PDF pages handled, by outcome (ocred, skipped)")
DURATION = Instrument("histogram", "ocr.duration", "s", "Time to OCR one PDF, by engine and outcome")
JOB_OUTCOMES = Instrument("counter", "ocr.job.outcome", "", "Outcome files written, by job and outcome")
TASK_DURATION = Instrument("histogram", "ocr.task.duration", "s", "Celery task run time")


def record_run(engine: str, outcome: str, seconds: float, pages: int | None, pages_ocred: int | None) -> None:
    """Pages: ``ocred`` / ``skipped`` (had text) for done and skipped runs; all pages ``failed`` for a failed run."""
    DURATION.record(seconds, {"engine": engine, "outcome": outcome})
    if not pages:
        return
    if outcome == "failed":
        PAGES.add(pages, {"outcome": "failed", "engine": engine})
        return
    if pages_ocred:
        PAGES.add(pages_ocred, {"outcome": "ocred", "engine": engine})
    if pages - (pages_ocred or 0) > 0:
        PAGES.add(pages - (pages_ocred or 0), {"outcome": "skipped", "engine": engine})


def record_outcome(status: str) -> None:
    """One outcome file written; ``status``: ``done``, ``skipped`` or ``failed``."""
    JOB_OUTCOMES.add(1, {"job": "ocr", "outcome": status})
