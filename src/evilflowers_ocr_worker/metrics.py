"""OCR's own metrics, declared with the kit's ``Instrument`` (scope ``evilflowers.ocr``; no-op without OTel).

The standard set comes from the kit (evilflowers-worker docs/architecture.md §4):
``ocr_task_duration_seconds``, ``ocr_job_outcome_total{job="ocr",outcome}`` (recorded
with ``record_job``) and ``ocr_deliveries_capped_total``. Next to them, under the same
prefix:

==========================  =================================================================
Prometheus name             Instrument
==========================  =================================================================
ocr_pages_total{outcome}    ``ocr.pages`` counter, pages by outcome (``ocred``, ``skipped``, ``failed``)
ocr_duration_seconds        ``ocr.duration`` histogram, one OCR run, by engine and outcome
==========================  =================================================================

Attributes stay low-cardinality: never a job or acquisition id (those go on spans and log lines).
"""

from evilflowers_worker.observability.metrics import Instrument, record_job

#: Seconds. One page (~2 s) up to a 4-hour book; the kit's default buckets stop at 2 h.
DURATION_BUCKETS = (1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 10800, 14400)

PAGES = Instrument("counter", "{service}.pages", "{page}", "PDF pages handled, by outcome (ocred, skipped, failed)")
DURATION = Instrument(
    "histogram", "{service}.duration", "s", "Time to OCR one PDF, by engine and outcome", buckets=DURATION_BUCKETS
)


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
    """One outcome file written: the kit's standard ``ocr_job_outcome_total{job="ocr",outcome}``."""
    record_job("ocr", status)
