"""Configuration of the worker: one typed object read from the environment (and `.env`).

The variable names are the ones issue #1 fixes (``BROKER``, ``STORAGE_PATH``,
``LOG_LEVEL``, the ``OTEL_*`` block); ``REDIS_URL`` is accepted too, as in the
sibling extraction workers. The worker talks to the catalog only through storage
and Celery events (issue #5): no catalog URL, no API key.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True, populate_by_name=True)

    # -- Celery -------------------------------------------------------------------------------------------------
    #: The broker: the catalog's Redis (dev: db 7 through the k8s-bridge).
    redis_url: str = Field("redis://redis:6379/7", validation_alias=AliasChoices("BROKER", "REDIS_URL"))
    #: The catalog's `EVILFLOWERS_OCR_QUEUE`.
    queue: str = Field("evilflowers_ocr_worker", validation_alias=AliasChoices("OCR_QUEUE", "QUEUE"))
    #: Soft limit: the run's own deadline. ocrmypdf stops between pages, the task writes a `failed`
    #: outcome (timeout). Below the catalog's EVILFLOWERS_OCR_TIME_LIMIT (4 h), after which its sweeper
    #: marks the job `lost`.
    task_soft_time_limit: int = 13_200
    #: Hard limit (Celery kills the child). The gap to the soft limit (15 min) must exceed one page's
    #: worst case (TESSERACT_TIMEOUT, 10 min) plus writing two files (the destination and the outcome,
    #: allowed 5 min), or a timeout never gets its outcome file.
    task_time_limit: int = 14_100
    #: Seconds before Redis redelivers an unacknowledged message. The task acknowledges late, so this
    #: must outlast the hard time limit. It is shared: the shortest value among a Redis db's consumers
    #: wins (knowledge#30), so it equals the catalog's and the extraction workers'
    #: (evilflowers_celery_visibility_timeout, 12 h). Never lower it on db 7.
    broker_visibility_timeout: int = 43_200
    #: Seconds a worker waits after SIGTERM before it cancels its task and Redis gets the message
    #: back (soft shutdown, celery >= 5.5; needs REMAP_SIGTERM=SIGQUIT, set in the Dockerfile).
    worker_soft_shutdown_timeout: float = 10.0
    worker_enable_soft_shutdown_on_idle: bool = True

    # -- Storage ------------------------------------------------------------------------------------------------
    #: The shared data volume; `source` and `destination` are relative to it.
    storage_path: Path = Path("/mnt/data")
    #: Scratch space for ocrmypdf (an emptyDir in k8s). None: the system temp dir ($TMPDIR, /tmp).
    tmp_dir: Path | None = None
    #: Built into the image by CI; reported as `service.version`.
    image_tag: str = "dev"

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
    #: `json` (one object per line, what Loki parses) or `text` (Celery's own format, for a terminal).
    log_format: Literal["json", "text"] = "json"
    log_level: str = "INFO"

    def storage_file(self, relative: str) -> Path:
        """Absolute path of a file named relative to the storage root; refuses anything that escapes it."""
        candidate = Path(relative)
        if not relative or candidate.is_absolute():
            raise ValueError(f"path must be relative to STORAGE_PATH: {relative!r}")
        root = self.storage_path.resolve()
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(root) or resolved == root:
            raise ValueError(f"path escapes STORAGE_PATH: {relative!r}")
        return resolved


@lru_cache
def get_settings() -> Settings:
    return Settings()
