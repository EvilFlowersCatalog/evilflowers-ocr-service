"""Configuration of the worker: the kit's ``RuntimeSettings`` plus OCR's own fields (issue #6).

``RuntimeSettings`` (evilflowers-worker) brings the broker, the visibility timeout,
the shutdown grace, the delivery cap (``MAX_DELIVERIES``), ``STORAGE_PATH`` with its
path guard (``storage_file``), ``IMAGE_TAG`` and ``LOG_FORMAT``. A catalog job
subclasses it, not ``WorkerSettings``: the worker talks to the catalog only through
storage and Celery events (issue #5), so it has no catalog URL and no API key.

The variable names are the ones issue #1 fixes (``BROKER``, ``OCR_QUEUE``,
``STORAGE_PATH``, ``LOG_LEVEL``, the ``OTEL_*`` block); ``REDIS_URL`` and ``QUEUE``
are accepted too, as in the sibling workers.
"""

from functools import lru_cache
from pathlib import Path

from evilflowers_worker.settings import RuntimeSettings
from pydantic import AliasChoices, Field

#: The pod's terminationGracePeriodSeconds (knowledge#54). The shutdown grace and the cancel must fit inside it.
POD_TERMINATION_GRACE = 30


class Settings(RuntimeSettings):
    # -- Broker (the kit's fields, under this service's variable names) -----------------------------------------
    #: The broker: the catalog's Redis (dev: db 7 through the k8s-bridge).
    redis_url: str = Field("redis://redis:6379/7", validation_alias=AliasChoices("BROKER", "REDIS_URL"))
    #: The catalog's `EVILFLOWERS_OCR_QUEUE`.
    queue: str = Field("evilflowers_ocr_worker", validation_alias=AliasChoices("OCR_QUEUE", "QUEUE"))

    # -- Time limits --------------------------------------------------------------------------------------------
    #: Soft limit: the run's own deadline. ocrmypdf stops between pages, the task writes a `failed`
    #: outcome (timeout). Below the catalog's EVILFLOWERS_OCR_TIME_LIMIT (4 h), after which its sweeper
    #: marks the job `lost`.
    task_soft_time_limit: int = 13_200
    #: Hard limit (Celery kills the child; set on the task). The gap to the soft limit (15 min) must exceed
    #: one page's worst case (TESSERACT_TIMEOUT, 10 min) plus writing two files (the destination and the
    #: outcome, allowed 5 min), or a timeout never gets its outcome file.
    task_time_limit: int = 14_100

    # -- Scale-in (the kit's `shutdown`) -------------------------------------------------------------------------
    #: Seconds a SIGTERMed worker lets the book run on before it cancels it and Redis has it back. Short on
    #: purpose: a book takes minutes to hours and almost never finishes inside any grace, so waiting longer
    #: only delays the requeue. Grace + cancel stay under the pod's terminationGracePeriodSeconds (30 s), with
    #: room for the fallback requeue's Redis call.
    worker_soft_shutdown_timeout: float = Field(10.0, ge=0)
    #: Seconds the cold cancel may take before the worker requeues the book itself and exits.
    worker_cancel_timeout: float = Field(10.0, ge=0)

    # -- Storage ------------------------------------------------------------------------------------------------
    #: Scratch space for ocrmypdf (an emptyDir in k8s). None: the system temp dir ($TMPDIR, /tmp).
    tmp_dir: Path | None = None

    # -- OCR engines --------------------------------------------------------------------------------------------
    #: Engine used when the task does not name one (the catalog does not, today).
    default_engine: str = Field("tesseract", validation_alias=AliasChoices("OCR_DEFAULT_ENGINE", "DEFAULT_ENGINE"))
    #: Engines a caller may choose; an engine not listed here is refused (`failed`).
    enabled_engines: tuple[str, ...] = Field(
        ("tesseract", "rapidocr"), validation_alias=AliasChoices("OCR_ENABLED_ENGINES", "ENABLED_ENGINES")
    )
    #: Pages OCR'd in parallel (ocrmypdf --jobs). Keep it equal to the pod's CPU limit.
    ocr_jobs: int = Field(1, ge=1, validation_alias=AliasChoices("OCR_JOBS", "JOBS"))
    #: Seconds Tesseract may spend on one page before ocrmypdf gives up on that page's text.
    tesseract_timeout: float = 600.0
    #: Where the RapidOCR models live (baked into the image). None: RapidOCR's package directory.
    rapidocr_model_dir: Path | None = Field(
        None, validation_alias=AliasChoices("OCRMYPDF_RAPIDOCR_MODEL_DIR", "RAPIDOCR_MODEL_DIR")
    )

    # -- Logs ---------------------------------------------------------------------------------------------------
    #: Level of the root logger. The deployment's command has no `--loglevel`, and Celery's default
    #: (WARNING) would hide every "OCR started / done" line.
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
