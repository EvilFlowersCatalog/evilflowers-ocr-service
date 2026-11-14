"""Fixtures: PDFs built on the fly (no binary fixtures in the repo) and isolated settings."""

import shutil
from pathlib import Path

import pikepdf
import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from PIL import Image, ImageDraw, ImageFont

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
        catalog_api_key="secret-key",
        report_attempts=5,
        report_backoff=(0.0,),
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
        "language": "eng",
        "redo": False,
        "report_url": "http://catalog.test/api/v1/jobs/runs/22222222-2222-2222-2222-222222222222/report",
    }
    base.update(overrides)
    return base
