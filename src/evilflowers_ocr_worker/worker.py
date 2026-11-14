"""The Celery application: ``celery -A evilflowers_ocr_worker.worker worker -E --queues=evilflowers_ocr_worker``.

=========================  ===========================  ==========================================
Task                       Queue                        Sent by
=========================  ===========================  ==========================================
evilflowers_ocr_worker.ocr ``OCR_QUEUE``                the catalog, job kind ``files.ocr`` (kwargs)
=========================  ===========================  ==========================================

``-E`` is required: the catalog builds the job's status from task events
(received, started, succeeded, failed, revoked); without them jobs stay queued and
become ``stale``. Nobody waits for a task's result (the task reports to the
catalog), so there is no result backend.
"""

from celery import Celery

from evilflowers_ocr_worker.observability.signals import connect
from evilflowers_ocr_worker.settings import Settings, get_settings

TASK_PREFIX = "evilflowers_ocr_worker"


class CeleryConfig:
    """Shared with the sibling extraction services (analyzer, image, text, equation) where it can be."""

    def __init__(self, settings: Settings):
        self.broker_url = settings.redis_url
        self.broker_connection_retry_on_startup = True
        # Acknowledged late: Redis must not redeliver a book still being OCR'd.
        self.broker_transport_options = {"visibility_timeout": settings.broker_visibility_timeout}

        self.task_serializer = "json"
        self.result_serializer = "json"
        self.accept_content = ["json"]
        self.timezone = "Europe/Bratislava"
        self.enable_utc = True

        self.task_default_queue = settings.queue
        self.task_routes = {f"{TASK_PREFIX}.*": {"queue": settings.queue}}
        self.worker_prefetch_multiplier = 1
        # A worker that dies mid-book (OOM, node drain) gets the message redelivered. The task
        # writes only its own `destination`, atomically, so a second delivery is safe.
        self.task_acks_late = True
        self.task_reject_on_worker_lost = True
        # Only the hard limit, below the catalog's EVILFLOWERS_OCR_TIME_LIMIT (default 4 h). The soft limit
        # (TASK_SOFT_TIME_LIMIT) is the run's own deadline, enforced between pages by ocr_hooks: Celery's
        # asynchronous SoftTimeLimitExceeded is absorbed by ocrmypdf's thread pool, and could otherwise land in
        # the middle of the report.
        self.task_time_limit = settings.task_time_limit
        # Safe scale-in: with REMAP_SIGTERM=SIGQUIT, SIGTERM is a cold shutdown that waits this long,
        # then hands the unacknowledged task back to the queue at once.
        self.worker_soft_shutdown_timeout = settings.worker_soft_shutdown_timeout
        self.worker_enable_soft_shutdown_on_idle = settings.worker_enable_soft_shutdown_on_idle
        # Monitoring (Flower, `celery events`) and the catalog's job ledger.
        self.worker_send_task_events = True
        self.task_send_sent_event = True


def create_app(settings: Settings | None = None) -> Celery:
    settings = settings or get_settings()
    app = Celery(TASK_PREFIX, include=["evilflowers_ocr_worker.tasks"])
    app.config_from_object(CeleryConfig(settings))
    return app


# Traces, metrics and JSON logs; off unless the pod points them at a collector.
connect()
app = create_app()
