"""The Celery application.

``celery -A evilflowers_ocr_worker.worker worker -E --queues=evilflowers.ocr,evilflowers_ocr_worker``

The image consumes ``evilflowers.ocr`` (the queue scheme of knowledge#14) next to the old ``evilflowers_ocr_worker``
while the catalog moves the ``files.ocr`` routing (issue #11); ``OCR_QUEUE`` still defaults to the old name.

=======================================  ===========================  ==========================================
Task                                     Queue                        Sent by
=======================================  ===========================  ==========================================
evilflowers_ocr_worker.ocr               ``OCR_QUEUE``                the catalog, job kind ``files.ocr`` (kwargs)
evilflowers_ocr_worker.standalone.hocr   ``OCR_QUEUE``                the books digitalizer (issue #7)
=======================================  ===========================  ==========================================

One image, two kinds of Deployment, each consuming its ``OCR_QUEUE``: the catalog's
(Redis db 7, ``evilflowers_ocr_worker``) and the digitalizer's (``BROKER`` on db 8,
``OCR_QUEUE=evilflowers_ocr_worker.standalone`` scaled by KEDA, and a second one with
``….standalone.interactive``, one replica). Producers name the queue in ``send_task``, so
the route below only matters for a send without one.

Built by the shared runtime, evilflowers-worker (issue #6): its ``create_app`` sets the
Celery configuration every worker on the db shares (prefetch 1, the 12 h visibility
timeout, task events), installs the scale-in protocol (``shutdown``: SIGTERM is a warm
shutdown, the book is cancelled and requeued after ``WORKER_SOFT_SHUTDOWN_TIMEOUT``) and
the telemetry. Late acknowledgement and the hard time limit are per task (``tasks``).

``-E`` is required: the catalog builds the job's status from task events
(received, started, succeeded, failed, revoked), and the events are what makes it
read the run's outcome file. Nobody waits for a task's result, so there is no result
backend.
"""

import logging
import os

from celery import signals
from evilflowers_worker import observability, runtime

from evilflowers_ocr_worker.settings import get_settings

TASK_PREFIX = "evilflowers_ocr_worker"
SERVICE = "ocr"

#: Span names of the fields `jobs` binds: the job ledger's (`evilflowers.job.id`, `evilflowers.run.id`,
#: `evilflowers.job.delivery`), plus OCR's. `evilflowers.acquisition.id` keeps the spelling the dashboards
#: use; the kit's canonical one is `evilflowers.acquisition_id` (its open question, knowledge#56).
ATTRIBUTES = {
    **observability.CATALOG_JOB_ATTRIBUTES,
    "acquisition_id": "evilflowers.acquisition.id",
    "engine": "ocr.engine",
    "language": "ocr.language",
    # The digitalizer's job within its run (standalone tasks).
    "job": "evilflowers.standalone.job",
}

settings = get_settings()

# `service.name` stays `ocr` (Tempo, Loki and the dashboards query it); the kit's default would be `evilflowers-ocr`.
os.environ.setdefault("OTEL_SERVICE_NAME", SERVICE)

app = runtime.create_app(
    SERVICE,
    settings,
    include=[f"{TASK_PREFIX}.tasks"],
    queues=[settings.queue],
    # The task name is a contract with the catalog (`OcrDefinition.task`), so the routes follow it.
    routes={f"{TASK_PREFIX}.*": settings.queue},
    attributes=ATTRIBUTES,
)


def _log_level(logger: logging.Logger | None = None, **_kwargs) -> None:
    """LOG_LEVEL on the worker's loggers: the deployment's command passes no `--loglevel` (Celery: WARNING)."""
    (logger or logging.getLogger()).setLevel(settings.log_level.upper())


signals.after_setup_logger.connect(_log_level, weak=False)
signals.after_setup_task_logger.connect(_log_level, weak=False)
