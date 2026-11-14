"""Operator commands.

``python -m evilflowers_ocr_worker engines``
    The OCR engines, their versions and languages (JSON).
``python -m evilflowers_ocr_worker selftest [-l eng rus]``
    OCR a generated page with every enabled engine and each language, in a temp
    directory. Exits non-zero if any run is not ``done``. Run it in the image with
    ``--read-only --network none`` to prove every model is baked in.
"""

import argparse
import json
import sys
import tempfile
import uuid
from pathlib import Path

from evilflowers_ocr_worker.engines import ENGINES
from evilflowers_ocr_worker.settings import get_settings

SELFTEST_LINES = [
    "The quick brown fox jumps over the lazy dog.",
    "Slovak University of Technology in Bratislava",
    "Library catalogue record number 1937",
]


def engines() -> int:
    settings = get_settings()
    listing = [
        {**engine.describe(), "enabled": name in settings.enabled_engines, "default": name == settings.default_engine}
        for name, engine in ENGINES.items()
    ]
    json.dump(listing, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def selftest(languages: list[str]) -> int:
    from PIL import Image, ImageDraw, ImageFont

    from evilflowers_ocr_worker import processing
    from evilflowers_ocr_worker.contract import OcrRequest

    base = get_settings()
    failures = 0
    with tempfile.TemporaryDirectory(prefix="ocr-selftest-") as scratch:
        storage = Path(scratch)
        page = Image.new("L", (1654, 2339), 255)
        draw = ImageDraw.Draw(page)
        font = ImageFont.load_default(size=44)
        for n, line in enumerate(SELFTEST_LINES * 4):
            draw.text((150, 200 + 80 * n), line, font=font, fill=0)
        page.save(storage / "scan.pdf", "PDF", resolution=200)
        settings = base.model_copy(update={"storage_path": storage, "tmp_dir": None})

        for name in settings.enabled_engines:
            for language in languages:
                run_id = uuid.uuid4().hex
                request = OcrRequest(
                    job_id=run_id,
                    acquisition_id=run_id,
                    source="scan.pdf",
                    destination=f"out/{name}-{language}.pdf",
                    language=language,
                    engine=name,
                )
                outcome = processing.run(request, settings, run_id)
                failures += outcome.status != "done"
                print(json.dumps({"engine": name, "language": language, **outcome.result()}))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evilflowers_ocr_worker")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("engines", help="list the OCR engines, their versions and languages (JSON)")
    test = commands.add_parser("selftest", help="OCR a generated page with every enabled engine")
    test.add_argument("-l", "--languages", nargs="+", default=["eng"], help="alpha3 codes (default: eng)")
    args = parser.parse_args(argv)

    if args.command == "engines":
        return engines()
    return selftest(args.languages)


if __name__ == "__main__":
    sys.exit(main())
