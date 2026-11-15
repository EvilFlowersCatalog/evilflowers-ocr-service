"""The Celery task. Its name is a contract with the catalog (`OcrDefinition.task`) and must not change.

Task options (issue #6): the kit's ``create_app`` sets none of these, so they are on the task.

- ``acks_late`` + ``reject_on_worker_lost`` (``runtime.JOB_TASK_OPTIONS``): a worker that dies mid-book (OOM,
  node drain) gets the message redelivered. Without them the task would be acknowledged on receipt and the book
  lost. The redelivery is safe: the task writes only its own `destination` and outcome file, atomically;
- ``time_limit``: Celery's hard kill, below the catalog's EVILFLOWERS_OCR_TIME_LIMIT. There is no Celery soft limit:
  the soft limit is the run's own deadline (``ocr_hooks``), because ocrmypdf's thread pool absorbs Celery's.

The body is ``runtime.run_task``: it validates the kwargs, counts the delivery and, past ``MAX_DELIVERIES`` (the
book killed its worker every time), calls ``jobs.give_up`` instead of OCRing it again.
"""

from typing import Any

from celery import shared_task
from evilflowers_worker import runtime
from pydantic import ValidationError

from evilflowers_ocr_worker import jobs
from evilflowers_ocr_worker import worker as _worker  # noqa: F401 - create_app must run in a spawned pool child too
from evilflowers_ocr_worker.contract import OcrRequest
from evilflowers_ocr_worker.settings import get_settings

TASK_NAME = "evilflowers_ocr_worker.ocr"


@shared_task(bind=True, name=TASK_NAME, time_limit=get_settings().task_time_limit, **runtime.JOB_TASK_OPTIONS)
def ocr(self, *_args, **kwargs) -> dict[str, Any]:
    """`files.ocr`: kwargs only (job_id, run_id, acquisition_id, source, destination, outcome, token, language,
    redo, and optionally engine). Unknown kwargs (`report_url` from an older catalog) are ignored."""
    task_id = self.request.id
    return runtime.run_task(
        self,
        lambda request, _delivery: jobs.run(request, task_id),
        OcrRequest,
        kwargs,
        on_capped=lambda request, delivery: jobs.give_up(request, task_id, delivery),
        on_invalid=_invalid,
    )


def _invalid(_kwargs: dict[str, Any], error: ValidationError) -> dict[str, Any]:
    """A message without `outcome`/`token` has nowhere to write: fail the task at once, never retried."""
    raise jobs.InvalidRequest(f"task kwargs do not match the files.ocr contract: {error}") from error
