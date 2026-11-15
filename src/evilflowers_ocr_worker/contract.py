"""What the catalog sends (issue #1, IP-031 revision 2) and the outcome file the worker writes back (issue #5).

Kwargs only. Unknown kwargs are ignored, so the catalog can add fields without
breaking an older worker, and a catalog that still sends ``report_url`` is not
refused. ``engine`` is this service's extension: the catalog does not send it yet,
and a worker without it falls back to ``OCR_DEFAULT_ENGINE``.

The outcome contract is the catalog's (EvilFlowersCatalog#169): the worker writes
``OcrOutcome`` as JSON at ``outcome``, the catalog reads it on the Celery event
(or on its reconcile tick) and applies it if ``run_id`` and ``token`` match the run.
"""

from typing import Literal

from evilflowers_worker.outcome import OutcomeFile
from pydantic import BaseModel, ConfigDict, Field

OutcomeStatus = Literal["done", "skipped", "failed"]


class OcrRequest(BaseModel):
    # hide_input_in_errors: a validation error must not echo the kwargs (and so the token) into the job's error.
    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)

    #: The job in the catalog's ledger (`/api/v1/jobs/<job_id>`).
    job_id: str
    #: The run; equals the Celery task id. Older catalogs did not send it.
    run_id: str | None = None
    #: The catalog file being OCR'd.
    acquisition_id: str
    #: Storage-relative path of the original PDF. Never written.
    source: str
    #: Storage-relative path to write: catalogs/<catalog>/<entry>/ocr/<run_id>.pdf.
    destination: str
    #: Storage-relative path of the outcome file: catalogs/<catalog>/<entry>/ocr/<run_id>.json.
    outcome: str
    #: The run's capability token, minted by the catalog. Opaque here: echoed back in the outcome file, never logged.
    token: str = Field(repr=False)
    #: ISO 639-2 (alpha3) of the entry, e.g. "slk", "ces", "eng".
    language: str
    #: Redo OCR on pages that already have text (ocrmypdf `redo` mode).
    redo: bool = False
    #: OCR engine (`tesseract`, `rapidocr`); None: the worker's default.
    engine: str | None = None


class OcrOutcome(OutcomeFile):
    """The outcome file: the kit's ``run_id`` / ``token`` / ``status`` plus OCR's fields (EvilFlowersCatalog#169).

    The kit's base forbids extra fields, so a misspelt key fails here, not in the catalog.
    """

    token: str = Field(repr=False)
    status: OutcomeStatus
    output: str | None = None
    pages: int | None = None
    pages_ocred: int | None = None
    skipped_reason: str | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    engine: str | None = None
    engine_version: str | None = None
    language: str | None = None
    seconds: float = 0.0
