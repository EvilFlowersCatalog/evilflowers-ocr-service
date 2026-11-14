"""Issue #2: real ocrmypdf runs on generated PDFs."""

import hashlib
from pathlib import Path

import pytest

from conftest import (
    kwargs,
    needs_tesseract,
    write_born_digital,
    write_encrypted,
    write_mixed,
    write_owner_locked,
    write_scanned,
)
from evilflowers_ocr_worker import processing
from evilflowers_ocr_worker.contract import OcrRequest
from evilflowers_ocr_worker.engines import RapidOcrEngine

RUN = "22222222-2222-2222-2222-222222222222"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def request(**overrides) -> OcrRequest:
    return OcrRequest.model_validate(kwargs(**overrides))


def leftovers(directory: Path) -> list[Path]:
    return [path for path in directory.rglob("*") if path.name.endswith(processing.PARTIAL_SUFFIX)]


@pytest.mark.ocr
@needs_tesseract
def test_scanned_pdf_is_done_and_source_untouched(settings, storage):
    req = request()
    source = write_scanned(storage / req.source)
    before = digest(source)

    outcome = processing.run(req, settings, RUN)

    assert outcome.status == "done", outcome.error
    assert outcome.pages == 1
    assert outcome.pages_ocred == 1
    assert outcome.output == req.destination
    assert outcome.engine == "tesseract"
    destination = storage / req.destination
    assert destination.is_file()
    assert digest(source) == before
    assert not leftovers(storage)
    assert not list(settings.tmp_dir.iterdir()), "the scratch directory is cleaned up"

    import pikepdf

    with pikepdf.open(destination) as pdf:
        assert len(pdf.pages) == 1
    from pdfminer.high_level import extract_text

    assert "quick brown fox" in extract_text(str(destination))


@pytest.mark.ocr
@needs_tesseract
def test_born_digital_pdf_is_skipped_without_destination(settings, storage):
    req = request()
    write_born_digital(storage / req.source)

    outcome = processing.run(req, settings, RUN)

    assert outcome.status == "skipped"
    assert outcome.pages == 1
    assert outcome.pages_ocred == 0
    assert outcome.skipped_reason == "all pages already have text"
    assert not (storage / req.destination).exists()


@pytest.mark.ocr
@needs_tesseract
def test_mixed_pdf_counts_only_the_ocred_page(settings, storage, tmp_path):
    req = request()
    write_mixed(storage / req.source, tmp_path)

    outcome = processing.run(req, settings, RUN)

    assert outcome.status == "done", outcome.error
    assert (outcome.pages, outcome.pages_ocred) == (2, 1)


@pytest.mark.ocr
@needs_tesseract
def test_redo_ocrs_pages_that_already_have_text(settings, storage, tmp_path):
    req = request(redo=True)
    write_mixed(storage / req.source, tmp_path)

    outcome = processing.run(req, settings, RUN)

    assert outcome.status == "done", outcome.error
    assert (outcome.pages, outcome.pages_ocred) == (2, 2)


@pytest.mark.ocr
@needs_tesseract
def test_multi_page_scan(settings, storage):
    req = request()
    write_scanned(storage / req.source, pages=3)
    outcome = processing.run(req, settings, RUN)
    assert (outcome.status, outcome.pages, outcome.pages_ocred) == ("done", 3, 3)


@pytest.mark.ocr
@needs_tesseract
def test_deadline_abandons_remaining_pages(settings, storage):
    import time

    req = request()
    write_scanned(storage / req.source, pages=20)
    started = time.monotonic()

    outcome = processing.run(req, settings, RUN, deadline=time.monotonic() + 1.0)

    assert outcome.status == "failed", outcome
    assert "time limit" in outcome.error
    assert time.monotonic() - started < 15, "ocrmypdf must stop, not OCR all 20 pages"
    assert not (storage / req.destination).exists()
    assert not leftovers(storage)


def test_encrypted_pdf_fails(settings, storage, tmp_path):
    req = request()
    write_encrypted(storage / req.source, tmp_path)
    outcome = processing.run(req, settings, RUN)
    assert outcome.status == "failed"
    assert "encrypted" in outcome.error
    assert not (storage / req.destination).exists()


@pytest.mark.ocr
@needs_tesseract
def test_owner_password_only_pdf_is_ocred(settings, storage, tmp_path):
    """Permission flags without a user password: ocrmypdf 17 opens it and OCRs it (only a user password fails)."""
    req = request()
    write_owner_locked(storage / req.source, tmp_path)
    outcome = processing.run(req, settings, RUN)
    assert outcome.status == "done", outcome.error


def test_corrupt_input_fails(settings, storage):
    req = request()
    (storage / req.source).parent.mkdir(parents=True)
    (storage / req.source).write_bytes(b"%PDF-1.7\nthis is not a pdf at all")
    outcome = processing.run(req, settings, RUN)
    assert outcome.status == "failed"
    assert "PDF" in outcome.error


def test_missing_source_fails(settings):
    outcome = processing.run(request(), settings, RUN)
    assert outcome.status == "failed"
    assert "does not exist" in outcome.error


@needs_tesseract
def test_unknown_language_fails_instead_of_falling_back(settings, storage):
    req = request(language="xxq")
    write_scanned(storage / req.source)
    outcome = processing.run(req, settings, RUN)
    assert outcome.status == "failed"
    assert "xxq" in outcome.error


@pytest.mark.parametrize("destination", ["/etc/passwd", "../outside.pdf", "catalogs/../../outside.pdf", ""])
def test_paths_may_not_escape_storage(settings, storage, destination):
    req = request(destination=destination)
    write_scanned(storage / req.source)
    outcome = processing.run(req, settings, RUN)
    assert outcome.status == "failed"


def test_destination_equal_to_source_is_refused(settings, storage):
    req = request(destination="catalogs/cat/entry/book.pdf")
    write_scanned(storage / req.source)
    outcome = processing.run(req, settings, RUN)
    assert outcome.status == "failed"
    assert "never written" in outcome.error


def test_unknown_engine_fails(settings, storage):
    req = request(engine="abbyy")
    write_scanned(storage / req.source)
    outcome = processing.run(req, settings, RUN)
    assert outcome.status == "failed"
    assert "unknown OCR engine" in outcome.error


def test_disabled_engine_fails(settings, storage):
    req = request(engine="rapidocr")
    write_scanned(storage / req.source)
    only_tesseract = settings.model_copy(update={"enabled_engines": ("tesseract",)})
    outcome = processing.run(req, only_tesseract, RUN)
    assert outcome.status == "failed"
    assert "not enabled" in outcome.error


@needs_tesseract
def test_stale_partial_file_of_this_run_is_removed(settings, storage):
    req = request()
    write_scanned(storage / req.source)
    destination = storage / req.destination
    destination.parent.mkdir(parents=True)
    stale = processing._partial_path(destination, RUN)
    stale.write_bytes(b"half a file")

    outcome = processing.run(req, settings, RUN)

    assert outcome.status == "done"
    assert not stale.exists()


@pytest.mark.ocr
@pytest.mark.skipif(not RapidOcrEngine().available(), reason="rapidocr is not installed")
def test_rapidocr_engine(settings, storage):
    req = request(engine="rapidocr")
    write_scanned(storage / req.source)

    outcome = processing.run(req, settings, RUN)

    assert outcome.status == "done", outcome.error
    assert outcome.engine == "rapidocr"
    assert outcome.pages_ocred == 1
    from pdfminer.high_level import extract_text

    assert "brown fox" in extract_text(str(storage / req.destination))
