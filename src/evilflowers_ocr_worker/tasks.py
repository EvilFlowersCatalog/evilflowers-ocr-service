"""The Celery task. Its name is a contract with the catalog (`OcrDefinition.task`) and must not change."""

from celery import shared_task

from evilflowers_ocr_worker import jobs

TASK_NAME = "evilflowers_ocr_worker.ocr"


@shared_task(bind=True, name=TASK_NAME)
def ocr(self, *_args, **kwargs):
    """`files.ocr`: kwargs only (job_id, run_id, acquisition_id, source, destination, outcome, token, language,
    redo, and optionally engine). Unknown kwargs (`report_url` from an older catalog) are ignored."""
    return jobs.run(self.request.id, kwargs)
