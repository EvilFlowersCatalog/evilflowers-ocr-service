"""What the catalog sends (issue #1, IP-031 revision 2) and the outcome file the worker writes back (issue #5).

Kwargs only. Unknown kwargs are ignored, so the catalog can add fields without
breaking an older worker, and a catalog that still sends ``report_url`` is not
refused. ``engine`` is this service's extension: the catalog does not send it yet,
and a worker without it falls back to ``OCR_DEFAULT_ENGINE``.

The outcome contract is the catalog's (EvilFlowersCatalog#169): the worker writes
``OcrOutcome`` as JSON at ``outcome``, the catalog reads it on the Celery event
(or on its reconcile tick) and applies it if ``run_id`` and ``token`` match the run.
"""

from typing import Any, Literal

from evilflowers_worker.contract import StandaloneJob
from evilflowers_worker.outcome import OutcomeFile
from pydantic import BaseModel, ConfigDict, Field, model_validator

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


# -- Standalone (catalog-free) jobs: the books digitalizer (issues #7, #8) -------------------------------------------


class OutcomeAddress(BaseModel):
    """Just enough of a standalone message to write its outcome: an invalid message still gets a `failed` file."""

    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)

    run_id: str = Field(min_length=1)
    token: str = Field(min_length=1, repr=False)
    outcome: str = Field(min_length=1)


#: ``[x0, y0, x1, y1]`` in the cleaned page's pixels, x1 and y1 exclusive.
Box = tuple[int, int, int, int]


class HocrLeafRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    #: The leaf number (IA ``page_%06d`` id, the ``NNNN_hocr.html`` file name).
    leaf: int = Field(ge=0)
    #: The cleaned page image. Only read; it is shared.
    image: str
    dpi: int = Field(300, gt=0)
    #: Tesseract-style language spec, one or more codes joined by ``+`` (``slk+eng``). ISO 639-2/B codes work too.
    language: str
    #: Operator-confirmed areas (removed, kept as image), painted white on a private copy before OCR.
    mask: list[Box] = Field(default_factory=list)


class HocrRequest(StandaloneJob):
    """``evilflowers_ocr_worker.standalone.hocr``: the kit's ``StandaloneJob`` (evilflowers-worker#5) plus the leaves,
    up to 32 (bulk) or one (interactive).

    ``inputs`` is the kit's generic list of input paths. The digitalizer sends ``leaves`` only (its per-leaf dpi,
    language and mask do not fit a path list), so a message without ``inputs`` gets the leaves' images.
    """

    # hide_input_in_errors: a validation error must not echo the kwargs (and so the token) into the job's error.
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)

    #: Never logged: the kit's field shows in a repr.
    token: str = Field(min_length=1, repr=False)
    leaves: list[HocrLeafRequest] = Field(min_length=1)
    #: OCR engine (``tesseract``, ``rapidocr``); None: the worker's default.
    engine: str | None = None
    #: Run the suspicious-line check (issue #8); None: ``STANDALONE_LINE_CHECK``.
    line_check: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _inputs_from_leaves(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("inputs") and isinstance(data.get("leaves"), list):
            images = [leaf.get("image") for leaf in data["leaves"] if isinstance(leaf, dict)]
            return {**data, "inputs": [image for image in images if isinstance(image, str)]}
        return data


class LineReading(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    #: 0..100, the mean word confidence of the line.
    conf: float
    engine: str


class SuspiciousLine(BaseModel):
    """One flagged line (issue #8). Lines that are not suspicious are not listed; ``NNNN_lines.json`` has them all."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The hOCR line id (``line_000003_7``).
    line: str
    bbox: Box
    suspicious: bool = True
    #: Which signals fired: ``low_confidence_unknown_words``, ``broken_diacritics``, ``punctuation``, ``hyphenation``.
    reasons: list[str]
    #: The engine's reading first, then the second engine's (if one could read the line).
    readings: list[LineReading]
    #: The accepted text when the readings agree (it may repair broken diacritics); None when disputed.
    chosen: str | None
    disputed: bool
    #: The judge's (``decider``) answer (``a``, ``b``, ``neither``), its probability, ``model@revision`` and band
    #: (``keep``, ``review``, ``hide``; None without calibrated thresholds). All None when no judge answered.
    p: float | None = None
    model: str | None = None
    verdict: str | None = None
    band: str | None = None


class HocrLeafOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    leaf: int
    #: One ``ocr_page`` (IA id ``page_%06d``), relative to STORAGE_PATH.
    hocr: str
    #: Plain text, one line per OCR line.
    text: str
    #: 0..100 over words.
    mean_conf: float
    #: Every line with its confidence and check result (``NNNN_lines.json``), relative to STORAGE_PATH.
    lines: str
    suspicious: list[SuspiciousLine] = Field(default_factory=list)


class HocrOutcome(OutcomeFile):
    """The outcome file of ``standalone.hocr`` (the digitalizer's ``HocrOutcome``, evilflowers-ocr-service#7)."""

    token: str = Field(repr=False)
    status: Literal["done", "failed"]
    error: str | None = None
    leaves: list[HocrLeafOutcome] = Field(default_factory=list)
    engine: str | None = None
    engine_version: str | None = None
    #: Hunspell dictionaries the line check used; empty: no lexicon signal (none installed for the language).
    lexicon: list[str] = Field(default_factory=list)
    seconds: float = 0.0
