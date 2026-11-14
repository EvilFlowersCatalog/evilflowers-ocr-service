"""What the catalog sends (issue #1, IP-031 revision 2) and what the worker reports back (issue #3).

Kwargs only. Unknown kwargs are ignored, so the catalog can add fields without
breaking an older worker. ``engine`` is this service's extension: the catalog does
not send it yet, and a worker without it falls back to ``OCR_DEFAULT_ENGINE``.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ReportStatus = Literal["done", "skipped", "failed"]


class OcrRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

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
    #: ISO 639-2 (alpha3) of the entry, e.g. "slk", "ces", "eng".
    language: str
    #: Redo OCR on pages that already have text (ocrmypdf `redo` mode).
    redo: bool = False
    #: POST the outcome here.
    report_url: str | None = None
    #: OCR engine (`tesseract`, `rapidocr`); None: the worker's default.
    engine: str | None = None


class OcrReport(BaseModel):
    """The report body. Keys are the ones the catalog stores; nothing else goes in it."""

    status: ReportStatus
    output: str | None = None
    pages: int | None = None
    pages_ocred: int | None = None
    skipped_reason: str | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)

    def body(self) -> dict[str, Any]:
        return self.model_dump()
