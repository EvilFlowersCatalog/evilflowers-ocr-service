"""EvilFlowers OCR worker: the `files.ocr` job of the catalog (IP-031).

The package keeps the name of the old ``evilflowers-ocr-worker`` so the task name
``evilflowers_ocr_worker.ocr`` and the catalog's routes stay valid.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("evilflowers-ocr-service")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"
