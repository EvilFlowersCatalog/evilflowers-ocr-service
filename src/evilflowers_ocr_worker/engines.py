"""The OCR engines a caller can choose, behind one interface.

Every engine is an ocrmypdf OCR engine plugin (``ocrmypdf.ocr(..., ocr_engine=...)``),
so the rest of the pipeline is shared: rasterising, rotation, deskew, skipping pages
that already have text, grafting the invisible text layer onto the original page and
the output PDF. An engine only has to say which languages it reads, under which code,
and which extra ocrmypdf options it needs.

=========  ==========================================================  ==========================
Engine     What                                                        Languages
=========  ==========================================================  ==========================
tesseract  Tesseract 5 LSTM (default). Best on Slovak/Czech diacritics  the installed packs
rapidocr   PaddleOCR PP-OCRv6/v5 on ONNX Runtime, CPU only             ~50 Latin, Cyrillic, CJK…
=========  ==========================================================  ==========================

A new engine is one more ``Engine`` subclass in ``ENGINES``: an ocrmypdf plugin
(built in, or passed through ``plugins()``) and a language map.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from abc import ABC, abstractmethod
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from typing import Any, ClassVar

from evilflowers_ocr_worker.settings import Settings

logger = logging.getLogger(__name__)

#: ISO 639-2/B (bibliographic) codes and macrolanguage codes the catalog may hold, mapped to the
#: 639-2/T / Tesseract code. Everything else is passed through: for most languages the 639-2/T
#: code IS the Tesseract pack name (slk, ces, eng, deu, hun, pol, rus, ukr, fra …).
ALPHA3_ALIASES = {
    "slo": "slk",
    "cze": "ces",
    "ger": "deu",
    "fre": "fra",
    "dut": "nld",
    "rum": "ron",
    "gre": "ell",
    "alb": "sqi",
    "arm": "hye",
    "geo": "kat",
    "ice": "isl",
    "mac": "mkd",
    "may": "msa",
    "baq": "eus",
    "wel": "cym",
    "tib": "bod",
    "bur": "mya",
    "per": "fas",
    "chi": "chi_sim",
    "zho": "chi_sim",
    "nob": "nor",
    "nno": "nor",
}


class EngineError(Exception):
    """The engine cannot run this request (unknown, disabled, unavailable, or the language is not read)."""


def normalise_language(language: str) -> str:
    code = language.strip().lower()
    return ALPHA3_ALIASES.get(code, code)


class Engine(ABC):
    #: The name a caller passes in the task's `engine` kwarg.
    name: ClassVar[str]
    #: ocrmypdf's `ocr_engine` value.
    ocrmypdf_engine: ClassVar[str]
    description: ClassVar[str]

    @abstractmethod
    def available(self) -> bool:
        """The engine is installed in this image."""

    @abstractmethod
    def languages(self) -> frozenset[str]:
        """Engine language codes this worker can read."""

    @abstractmethod
    def version(self) -> str: ...

    def language(self, alpha3: str) -> str:
        """The engine's code for the entry's language. Never falls back to another language."""
        code = normalise_language(alpha3)
        if code not in self.languages():
            raise EngineError(f"OCR engine {self.name!r} cannot read language {alpha3!r} (no {code!r} model)")
        return code

    def options(self, settings: Settings) -> dict[str, Any]:
        """Extra ocrmypdf options for this engine."""
        return {}

    def describe(self) -> dict[str, Any]:
        available = self.available()
        return {
            "name": self.name,
            "description": self.description,
            "available": available,
            "version": self.version() if available else None,
            "languages": sorted(self.languages()) if available else [],
        }


class TesseractEngine(Engine):
    name = "tesseract"
    ocrmypdf_engine = "tesseract"
    description = "Tesseract 5 (LSTM). The default; the most accurate on Slovak and Czech diacritics."

    def available(self) -> bool:
        return shutil.which("tesseract") is not None

    def languages(self) -> frozenset[str]:
        return _tesseract_languages()

    def version(self) -> str:
        return _tesseract_version()

    def options(self, settings: Settings) -> dict[str, Any]:
        return {"tesseract_timeout": settings.tesseract_timeout}


class RapidOcrEngine(Engine):
    name = "rapidocr"
    ocrmypdf_engine = "rapidocr"
    description = (
        "PaddleOCR PP-OCRv6/v5 detection and recognition on ONNX Runtime (CPU). Robust on noisy scans and "
        "multi-column layouts; reads ť/ľ/ď less reliably than Tesseract."
    )

    def available(self) -> bool:
        return find_spec("rapidocr") is not None and find_spec("onnxruntime") is not None

    def languages(self) -> frozenset[str]:
        if not self.available():
            return frozenset()
        from ocrmypdf.builtin_plugins.rapidocr_ocr import RapidOcrEngine as Plugin

        return frozenset(Plugin.languages(None))

    def version(self) -> str:
        try:
            return f"rapidocr {version('rapidocr')}, onnxruntime {version('onnxruntime')}"
        except PackageNotFoundError:
            return "unknown"

    def options(self, settings: Settings) -> dict[str, Any]:
        if settings.rapidocr_model_dir is not None:
            return {"rapidocr_model_dir": settings.rapidocr_model_dir}
        return {}


ENGINES: dict[str, Engine] = {engine.name: engine for engine in (TesseractEngine(), RapidOcrEngine())}


def get_engine(name: str | None, settings: Settings) -> Engine:
    """The engine a request asks for, or the default. Unknown, disabled or missing is an error, never a fallback."""
    chosen = (name or settings.default_engine).strip().lower()
    engine = ENGINES.get(chosen)
    if engine is None:
        raise EngineError(f"unknown OCR engine {chosen!r}; known: {', '.join(sorted(ENGINES))}")
    if chosen not in settings.enabled_engines:
        raise EngineError(f"OCR engine {chosen!r} is not enabled on this worker (OCR_ENABLED_ENGINES)")
    if not engine.available():
        raise EngineError(f"OCR engine {chosen!r} is not installed in this image")
    return engine


@cache
def _tesseract_languages() -> frozenset[str]:
    try:
        listed = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True, check=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        logger.exception("tesseract --list-langs failed")
        return frozenset()
    # First line is the header ("List of available languages in ..."); `osd` is orientation, not a language.
    codes = {line.strip() for line in listed.splitlines()[1:] if line.strip()}
    return frozenset(code for code in codes if code != "osd" and "/" not in code)


@cache
def _tesseract_version() -> str:
    try:
        out = subprocess.run(["tesseract", "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    first = (out.stdout or out.stderr).splitlines()
    return first[0].strip() if first else "unknown"
