"""OCR of one PDF (issue #2): read `source`, write only `destination`, never half-written.

The old worker ran ``ocrmypdf.ocr(source, tmp)`` with default options and moved the
result over the original. That made the catalog's checksum stale, raced with LCP
encryption and extraction, and raised ``PriorOcrFoundError`` on every born-digital
PDF. Here:

- skip mode (``redo=False``): pages that already have text are left alone;
  redo mode (``redo=True``) replaces an old OCR layer. ocrmypdf refuses ``deskew``
  in redo mode, so it is only on in skip mode;
- ocrmypdf writes into a private temp directory; the file is then copied next to
  the destination under a ``.partial`` name, fsynced and renamed, so
  ``destination`` either does not exist or is complete;
- ``source`` is only ever opened for reading.

Outcomes: ``done`` (at least one page OCR'd, destination written), ``skipped``
(nothing to OCR, or a PDF ocrmypdf deliberately leaves alone; no destination),
``failed`` (encrypted, corrupt, unknown language, engine missing, timeout…).
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ocrmypdf
import pikepdf
from evilflowers_worker.outcome import PARTIAL_SUFFIX
from ocrmypdf import exceptions as ocr_errors
from PIL import Image

from evilflowers_ocr_worker import linecheck, ocr_hooks, standalone
from evilflowers_ocr_worker.contract import OcrOutcome, OcrRequest
from evilflowers_ocr_worker.engines import Engine, EngineError, get_engine
from evilflowers_ocr_worker.settings import Settings

logger = logging.getLogger(__name__)

#: ocrmypdf warnings kept in the outcome file, at most.
MAX_WARNINGS = 20


@dataclass
class Outcome:
    status: str
    pages: int | None = None
    pages_ocred: int | None = None
    output: str | None = None
    skipped_reason: str | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    engine: str | None = None
    engine_version: str | None = None
    language: str | None = None
    seconds: float = 0.0
    #: The exception behind a `failed` outcome; re-raised by the task so the job keeps the traceback.
    exception: BaseException | None = field(default=None, repr=False)

    def file(self, run_id: str, token: str) -> OcrOutcome:
        """The outcome file's contents (EvilFlowersCatalog#169)."""
        return OcrOutcome(
            run_id=run_id,
            token=token,
            status=self.status,  # type: ignore[arg-type]
            output=self.output,
            pages=self.pages,
            pages_ocred=self.pages_ocred,
            skipped_reason=self.skipped_reason,
            error=self.error,
            warnings=self.warnings,
            engine=self.engine,
            engine_version=self.engine_version,
            language=self.language,
            seconds=round(self.seconds, 3),
        )

    def result(self) -> dict[str, Any]:
        """The task's return value, kept compact: Celery's event carries a repr capped at 1024 characters.

        The full outcome (warnings, errors) lives in the outcome file.
        """
        return {
            "status": self.status,
            "pages": self.pages,
            "pages_ocred": self.pages_ocred,
            "engine": self.engine,
            "seconds": round(self.seconds, 3),
        }


class _WarningCollector(logging.Handler):
    """Keeps ocrmypdf's warnings (from every worker thread) for the outcome file."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage().strip()
        if message and message not in self.messages and len(self.messages) < MAX_WARNINGS:
            self.messages.append(message)


#: Exceptions ocrmypdf raises on purpose for PDFs it leaves alone; skipped, not failed.
SKIPPED_ERRORS: dict[type[BaseException], str] = {
    ocr_errors.PriorOcrFoundError: "the PDF already has text",
    ocr_errors.TaggedPDFError: "tagged PDF (born-digital); ocrmypdf leaves it alone",
    ocr_errors.DigitalSignatureError: "digitally signed PDF; OCR would invalidate the signature",
}


def run(request: OcrRequest, settings: Settings, run_id: str, deadline: float | None = None) -> Outcome:
    """OCR one request. ``deadline`` (``time.monotonic()``): past it, remaining pages are abandoned → failed."""
    started = time.perf_counter()
    outcome = _run(request, settings, run_id, deadline)
    outcome.seconds = time.perf_counter() - started
    return outcome


def _run(request: OcrRequest, settings: Settings, run_id: str, deadline: float | None) -> Outcome:
    # -- the request itself -------------------------------------------------------------------------------------
    try:
        engine = get_engine(request.engine, settings)
        language = engine.language(request.language)
        source = settings.storage_file(request.source)
        destination = settings.storage_file(request.destination)
    except (EngineError, ValueError) as error:
        return Outcome("failed", error=str(error), engine=request.engine or settings.default_engine, exception=error)
    if source == destination:
        error = ValueError("destination equals source; the original is never written")
        return Outcome("failed", error=str(error), engine=engine.name, exception=error)

    base = {"engine": engine.name, "engine_version": engine.version(), "language": language}
    if not source.is_file():
        error = FileNotFoundError(f"source does not exist: {request.source}")
        return Outcome("failed", error=str(error), exception=error, **base)

    # -- page count (also a cheap check that the file opens) ---------------------------------------------------
    try:
        with pikepdf.open(source) as pdf:
            pages = len(pdf.pages)
    except pikepdf.PasswordError as error:
        return Outcome("failed", error="encrypted PDF (password required)", exception=error, **base)
    except pikepdf.PdfError as error:
        return Outcome("failed", error=f"not a readable PDF: {error}", exception=error, **base)

    # A previous delivery killed mid-copy may have left a partial file; it is ours to remove.
    _remove(_partial_path(destination, run_id))

    # -- OCR ------------------------------------------------------------------------------------------------
    with tempfile.TemporaryDirectory(prefix=f"ocr-{run_id}-", dir=settings.tmp_dir) as scratch:
        output = Path(scratch) / "output.pdf"
        collector = _WarningCollector()
        ocr_logger = logging.getLogger("ocrmypdf")
        ocr_logger.addHandler(collector)
        pages_dir = Path(scratch) / "pages" if settings.ocr_line_check else None
        if pages_dir is not None:
            pages_dir.mkdir()
        ocr_hooks.start(str(output), deadline, keep_images=pages_dir)
        try:
            try:
                with ocr_hooks.interruptible(str(output)):
                    _ocrmypdf(source, output, engine, language, request.redo, settings)
            except Exception as error:
                # Stopping: no outcome file, never ack. The process exits and Redis redelivers the message.
                if ocr_hooks.is_aborted(str(output)):
                    raise SystemExit("terminated during OCR") from error
                # However ocrmypdf surfaces a page that hit the deadline (raised, wrapped, or an exit code),
                # past the deadline it is a timeout.
                if deadline is not None and time.monotonic() > deadline:
                    raise ocr_hooks.DeadlineExceeded(str(error)) from error
                raise
        except ocr_hooks.DeadlineExceeded as error:
            message = f"time limit exceeded ({settings.task_soft_time_limit} s); remaining pages abandoned"
            return Outcome("failed", pages=pages, error=message, exception=error, **base)
        except tuple(SKIPPED_ERRORS) as error:
            reason = next(text for kind, text in SKIPPED_ERRORS.items() if isinstance(error, kind))
            return Outcome("skipped", pages=pages, pages_ocred=0, skipped_reason=reason, **base)
        except ocr_errors.EncryptedPdfError as error:
            return Outcome("failed", pages=pages, error="encrypted PDF", exception=error, **base)
        except ocr_errors.MissingDependencyError as error:
            return Outcome("failed", pages=pages, error=f"missing dependency: {error}", exception=error, **base)
        except ocr_errors.BadArgsError as error:
            return Outcome("failed", pages=pages, error=f"bad OCR options: {error}", exception=error, **base)
        except ocr_errors.InputFileError as error:
            return Outcome("failed", pages=pages, error=f"input not accepted: {error}", exception=error, **base)
        except ocr_errors.ExitCodeException as error:
            message = str(error) or type(error).__name__
            return Outcome("failed", pages=pages, error=f"ocrmypdf: {message}", exception=error, **base)
        finally:
            ocred = ocr_hooks.finish(str(output))
            ocr_logger.removeHandler(collector)

        warnings = collector.messages
        if not ocred:
            return Outcome(
                "skipped", pages=pages, pages_ocred=0, warnings=warnings, skipped_reason=_skip_reason(request), **base
            )
        if pages_dir is not None:
            checked = _line_check(pages_dir, engine, request.language, settings, deadline, Path(scratch))
            warnings = [*warnings, *checked]
        _publish(output, destination, run_id)

    logger.info("OCR done: %d of %d pages OCR'd, written to %s", len(ocred), pages, request.destination)
    return Outcome("done", pages=pages, pages_ocred=len(ocred), output=request.destination, warnings=warnings, **base)


def _ocrmypdf(source: Path, output: Path, engine: Engine, language: str, redo: bool, settings: Settings) -> None:
    result = ocrmypdf.ocr(
        source,
        output,
        language=[language],
        ocr_engine=engine.ocrmypdf_engine,
        mode="redo" if redo else "skip",
        rotate_pages=True,
        # ocrmypdf: redo mode is not compatible with deskew.
        deskew=not redo,
        output_type="pdf",
        jobs=settings.ocr_jobs,
        # Threads, not processes: a Celery prefork child is daemonic and may not fork a pool, and
        # ocr_hooks counts pages and checks the deadline in this process.
        use_threads=True,
        progress_bar=False,
        plugins=["evilflowers_ocr_worker.ocr_hooks"],
        **engine.options(settings),
    )
    if result != ocrmypdf.ExitCode.ok:
        raise ocr_errors.ExitCodeException(f"exit code {int(result)} ({result.name})")


def _line_check(
    pages_dir: Path, engine: Engine, language: str, settings: Settings, deadline: float | None, scratch: Path
) -> list[str]:
    """Suspicious lines of the OCR'd pages as warnings (``OCR_LINE_CHECK``, issue #8). Advisory: it never fails
    the run. ocrmypdf keeps no per-line confidence, so every page is OCR'd once more, to hOCR, and checked."""
    found: list[tuple[int, Any]] = []
    stopped = None
    try:
        languages = engine.languages_for(language)
        ask = standalone.judge(settings)
        for image in sorted(pages_dir.glob("*.png")):
            number = int(image.stem) + 1
            with Image.open(image) as opened:
                dpi = opened.info.get("dpi", (300, 300))
            _page, check, _lexicon = standalone.read_page(
                image,
                leaf=number,
                dpi=round(float(dpi[0] if isinstance(dpi, tuple) else dpi)),
                engine=engine,
                language=language,
                languages=languages,
                settings=settings,
                line_check=True,
                scratch=scratch,
                deadline=deadline,
                ask=ask,
            )
            found.extend((number, line) for line in check.suspicious)
    except ocr_hooks.DeadlineExceeded:
        stopped = "line check stopped at the time limit; later pages were not checked"
    except Exception as error:  # Exception only: a stop (SystemExit) propagates as everywhere else.
        logger.warning("Line check failed", exc_info=True)
        stopped = f"line check failed: {type(error).__name__}: {error}"
    disputed = sum(line.disputed for _number, line in found)
    head = f"line check: {len(found)} suspicious lines, {disputed} disputed"
    return [head, *linecheck.summary(found)[:MAX_WARNINGS], *([stopped] if stopped else [])]


def _skip_reason(request: OcrRequest) -> str:
    if request.redo:
        return "no page needed OCR"
    return "all pages already have text"


def _partial_path(destination: Path, run_id: str) -> Path:
    return destination.with_name(f".{destination.name}.{run_id}{PARTIAL_SUFFIX}")


def _publish(output: Path, destination: Path, run_id: str) -> None:
    """Copy onto the shared volume under a temporary name, fsync, rename: never a half-written destination.

    The same protocol as the kit's ``outcome.atomic_write``, but streamed: that one takes ``bytes``, and an
    OCR'd book can be hundreds of megabytes. The outcome file itself goes through the kit (``jobs``).
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = _partial_path(destination, run_id)
    try:
        with output.open("rb") as reader, partial.open("wb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(partial, destination)
    except BaseException:
        _remove(partial)
        raise
    _fsync_directory(destination.parent)


def _fsync_directory(directory: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _remove(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
