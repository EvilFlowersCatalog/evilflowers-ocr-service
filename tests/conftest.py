"""Fixtures: PDFs built on the fly (no binary fixtures in the repo) and isolated settings."""

import json
import os
import shutil
from pathlib import Path

# Before the app is created: in-process tests never reach a real broker (the delivery counter fails fast and
# counts every delivery as the first). The e2e tests start their own worker with TEST_REDIS_URL.
os.environ["BROKER"] = "redis://127.0.0.1:1/0"
os.environ.setdefault("LOG_FORMAT", "text")

import pikepdf
import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from PIL import Image, ImageDraw, ImageFont

# First, so the kit's identity is `ocr` before any instrument or span name is resolved (they are cached).
from evilflowers_ocr_worker import worker  # noqa: F401
from evilflowers_ocr_worker.engines import TesseractEngine
from evilflowers_ocr_worker.settings import Settings

# One SDK provider per process (OpenTelemetry refuses to replace it), recording in memory.
SPANS = InMemorySpanExporter()
METRICS = InMemoryMetricReader()
_tracer_provider = TracerProvider()
_tracer_provider.add_span_processor(SimpleSpanProcessor(SPANS))
trace.set_tracer_provider(_tracer_provider)
metrics.set_meter_provider(MeterProvider(metric_readers=[METRICS]))

LINES = [
    "The quick brown fox jumps over the lazy dog.",
    "Slovak University of Technology in Bratislava",
    "Faculty of Informatics and Information Technologies",
    "Library catalogue record number 1937",
]

HAS_TESSERACT = shutil.which("tesseract") is not None
needs_tesseract = pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract is not installed")
#: The standalone hOCR tests read Slovak (CI installs tesseract-ocr-slk; the image has it).
needs_slovak = pytest.mark.skipif(
    not HAS_TESSERACT or "slk" not in TesseractEngine().languages(), reason="tesseract has no slk pack"
)


@pytest.fixture
def storage(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


@pytest.fixture
def settings(storage: Path, tmp_path: Path) -> Settings:
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    return Settings(
        storage_path=storage,
        tmp_dir=scratch,
        log_format="text",
        ocr_jobs=1,
        _env_file=None,
    )


def scanned_page(lines: list[str] = LINES) -> Image.Image:
    """An A4 page at 200 dpi holding only an image of text, like a scanner produces."""
    page = Image.new("L", (1654, 2339), 255)
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=44)
    y = 200
    for line in lines * 3:
        draw.text((150, y), line, font=font, fill=0)
        y += 80
    return page


#: Slovak with every caron and acute the engines get wrong (ť ľ ď č ž š ň, á é í ó ú ý ô ä ĺ ŕ).
SLOVAK_LINES = [
    "Ťažký ľadový čaj stál na stole.",
    "Ďalšie úlohy riešila Žofia v piatok.",
    "Kôň skákal ponad dlhý múr.",
    "Fakulta informatiky v Bratislave",
]

#: TrueType fonts with Latin Extended-A (ť, ľ, ď): the Debian image's, then macOS's.
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
]


def slovak_font(size: int = 48):
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    pytest.skip("no TrueType font with Slovak diacritics on this machine")


def slovak_leaf(path: Path, lines: list[str] = SLOVAK_LINES, dpi: int = 300) -> Path:
    """A cleaned leaf as the digitalizer hands it over: an A5 page image at ``dpi``, black text on white."""
    page = Image.new("L", (1748, 2480), 255)
    draw = ImageDraw.Draw(page)
    font = slovak_font()
    y = 300
    for line in lines:
        draw.text((180, y), line, font=font, fill=0)
        y += 110
    path.parent.mkdir(parents=True, exist_ok=True)
    page.save(path, dpi=(dpi, dpi))
    return path


def write_scanned(path: Path, pages: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    images = [scanned_page() for _ in range(pages)]
    images[0].save(path, "PDF", resolution=200, save_all=True, append_images=images[1:])
    return path


def write_born_digital(path: Path) -> Path:
    from fpdf import FPDF

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=14)
    for line in LINES:
        pdf.cell(0, 10, line, new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))
    return path


def write_mixed(path: Path, tmp: Path) -> Path:
    """Page 1 scanned, page 2 born-digital."""
    scanned = write_scanned(tmp / "mixed-scan.pdf")
    digital = write_born_digital(tmp / "mixed-digital.pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.open(scanned) as first, pikepdf.open(digital) as second:
        first.pages.extend(second.pages)
        first.save(path)
    return path


def write_encrypted(path: Path, tmp: Path) -> Path:
    plain = write_scanned(tmp / "plain.pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.open(plain) as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(owner="owner", user="user", R=6))
    return path


def write_owner_locked(path: Path, tmp: Path) -> Path:
    """Encrypted with an owner password only: it opens without a password, ocrmypdf still refuses it."""
    plain = write_scanned(tmp / "plain-owner.pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.open(plain) as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(owner="owner", user="", R=6))
    return path


def kwargs(**overrides):
    base = {
        "job_id": "11111111-1111-1111-1111-111111111111",
        "run_id": "22222222-2222-2222-2222-222222222222",
        "acquisition_id": "33333333-3333-3333-3333-333333333333",
        "source": "catalogs/cat/entry/book.pdf",
        "destination": "catalogs/cat/entry/ocr/22222222-2222-2222-2222-222222222222.pdf",
        "outcome": "catalogs/cat/entry/ocr/22222222-2222-2222-2222-222222222222.json",
        "token": TOKEN,
        "language": "eng",
        "redo": False,
    }
    base.update(overrides)
    return base


TOKEN = "cap-token-4f1c9a"


def read_outcome(storage: Path, args: dict) -> dict:
    return json.loads((storage / args["outcome"]).read_text())
