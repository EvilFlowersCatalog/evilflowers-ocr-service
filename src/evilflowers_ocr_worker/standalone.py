"""``evilflowers_ocr_worker.standalone.hocr``: leaf images → one hOCR per leaf, for the books digitalizer (issue #7).

The catalog-free twin of ``files.ocr``: the same engines (``engines``), no PDF and no
ocrmypdf pipeline, no catalog. The message is a ``HocrRequest`` (``contract``); its
paths are relative to ``STORAGE_PATH``.

Per leaf, in order:

1. the image is only read; the operator's ``mask`` boxes are painted white on a private
   copy in scratch space (x1, y1 exclusive);
2. the engine OCRs it to hOCR (``Engine.recognize``) in every language of the spec
   (``slk+eng``); a language the engine does not read fails the job, never falls back;
3. the suspicious-line check (``linecheck``, issue #8) flags lines and reads them again
   with the other engine;
4. ``NNNN_hocr.html`` (one ``ocr_page``, id ``page_%06d``) and ``NNNN_lines.json``
   (every line with its confidence and verdict) are written atomically into ``output_dir``.

Then the outcome file, last and atomically (the kit's ``write_outcome``): it is the
commit marker the orchestrator promotes the job by. The outcome rules are the kit's
(``evilflowers_worker.outcome``): ``failed`` is written and then raised (Celery fires
``link_error``); a deliberate stop (SIGTERM, cold cancel: ``SystemExit``) writes nothing,
so the redelivery can; past ``MAX_DELIVERIES``, ``give_up`` writes ``failed``.

Time: the run's deadline (``TASK_SOFT_TIME_LIMIT``, as in ``files.ocr``) is checked
before every leaf and every second reading, and bounds each Tesseract process, so a
batch stops within one page's time and still writes its outcome before Celery's hard
limit.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from evilflowers_worker import observability
from evilflowers_worker.judges import Judges
from evilflowers_worker.observability.metrics import record_job
from evilflowers_worker.outcome import atomic_write, write_outcome
from PIL import Image, ImageDraw

from evilflowers_ocr_worker import hocr, linecheck
from evilflowers_ocr_worker.contract import (
    Box,
    HocrLeafOutcome,
    HocrLeafRequest,
    HocrOutcome,
    HocrRequest,
    LineReading,
    OutcomeAddress,
)
from evilflowers_ocr_worker.engines import ENGINES, Engine, EngineError, get_engine
from evilflowers_ocr_worker.ocr_hooks import DeadlineExceeded
from evilflowers_ocr_worker.settings import Settings, get_settings

logger = logging.getLogger(__name__)

TASK_NAME = "evilflowers_ocr_worker.standalone.hocr"
#: The ``job`` label of the kit's ``ocr_job_outcome_total``.
METRIC_JOB = "hocr"
#: Pixels around a line's box in its crop for the second reading.
LINE_PADDING = 6


class InvalidRequest(ValueError):
    """The message has nowhere to write its outcome."""


class StandaloneFailed(RuntimeError):
    """The outcome is `failed`; raised after the outcome file is written, so the task fails (``link_error``)."""


class GaveUp(RuntimeError):
    """Past MAX_DELIVERIES: the job killed its worker on every delivery; raised after the `failed` outcome."""


def run(request: HocrRequest, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    check_outcome_path(request, settings)
    started = time.perf_counter()
    deadline = time.monotonic() + settings.task_soft_time_limit
    engine_name = request.engine or settings.default_engine

    with (
        observability.span("ocr.standalone.hocr") as span,
        observability.bind(run_id=request.run_id, job=request.job, engine=engine_name),
    ):
        logger.info("hOCR started: %d leaves -> %s", len(request.leaves), request.output_dir)
        try:
            outcome = _process(request, settings, deadline)
        # Exception only: SystemExit (a deliberate stop) must propagate and never become a `failed` outcome.
        except Exception as error:
            logger.exception("hOCR failed")
            message = _message(error, settings)
            write_failed(request, settings, message, engine=engine_name, seconds=time.perf_counter() - started)
            if span is not None:
                observability.fail(error)
            raise StandaloneFailed(message) from error
        outcome = outcome.model_copy(update={"seconds": round(time.perf_counter() - started, 3)})
        write_outcome(settings, request.outcome, outcome)
        record_job(METRIC_JOB, "done")
        flagged = sum(len(leaf.suspicious) for leaf in outcome.leaves)
        logger.info(
            "hOCR done: %d leaves, %d suspicious lines (outcome: %s)", len(outcome.leaves), flagged, request.outcome
        )
        return {"job": request.job, "status": "done", "leaves": len(outcome.leaves), "suspicious": flagged}


def give_up(request: HocrRequest, delivery: int, settings: Settings | None = None) -> None:
    """Past the delivery cap: write a `failed` outcome (the orchestrator ends the job, it does not re-send it)."""
    settings = settings or get_settings()
    check_outcome_path(request, settings)
    error = f"gave up after {delivery - 1} deliveries: the worker died while OCRing these leaves every time"
    with observability.bind(run_id=request.run_id, job=request.job):
        write_failed(request, settings, error, engine=request.engine or settings.default_engine)
        logger.error("hOCR %s (outcome: %s)", error, request.outcome)
        raise GaveUp(error)


def write_failed(
    request: HocrRequest | OutcomeAddress,
    settings: Settings,
    error: str,
    *,
    engine: str | None = None,
    seconds: float = 0.0,
) -> None:
    outcome = HocrOutcome(
        run_id=request.run_id,
        token=request.token,
        status="failed",
        error=error,
        engine=engine,
        seconds=round(seconds, 3),
    )
    write_outcome(settings, request.outcome, outcome)
    record_job(METRIC_JOB, "failed")


def check_outcome_path(request: HocrRequest | OutcomeAddress, settings: Settings) -> Path:
    try:
        return settings.storage_file(request.outcome)
    except ValueError as error:
        raise InvalidRequest(f"outcome: {error}") from error


# -- one job --------------------------------------------------------------------------------------------------------


def _process(request: HocrRequest, settings: Settings, deadline: float) -> HocrOutcome:
    engine = get_engine(request.engine, settings)
    output_dir = request.prepare_output(settings)
    line_check = settings.standalone_line_check if request.line_check is None else request.line_check
    ask = judge(settings) if line_check else None
    leaves: list[HocrLeafOutcome] = []
    dictionaries: list[str] = []
    with tempfile.TemporaryDirectory(prefix=f"hocr-{_safe(request.job)}-", dir=settings.tmp_dir) as scratch:
        for leaf in request.leaves:
            _check_deadline(deadline)
            result, used = _leaf(leaf, engine, output_dir, Path(scratch), settings, deadline, line_check, ask)
            leaves.append(result)
            dictionaries.extend(name for name in used if name not in dictionaries)
    return HocrOutcome(
        run_id=request.run_id,
        token=request.token,
        status="done",
        leaves=leaves,
        engine=engine.name,
        engine_version=engine.version(),
        lexicon=dictionaries,
    )


def _leaf(
    leaf: HocrLeafRequest,
    engine: Engine,
    output_dir: Path,
    scratch: Path,
    settings: Settings,
    deadline: float,
    line_check: bool,
    ask: linecheck.Ask | None,
) -> tuple[HocrLeafOutcome, list[str]]:
    languages = engine.languages_for(leaf.language)
    source = settings.storage_file(leaf.image)
    if not source.is_file():
        raise FileNotFoundError(f"leaf {leaf.leaf}: image does not exist: {leaf.image}")
    image = masked(source, leaf.mask, scratch / f"{leaf.leaf:04d}-masked.png", leaf.dpi)
    page, check, lexicon = read_page(
        image,
        leaf=leaf.leaf,
        dpi=leaf.dpi,
        engine=engine,
        language=leaf.language,
        languages=languages,
        settings=settings,
        line_check=line_check,
        scratch=scratch,
        deadline=deadline,
        ask=ask,
    )
    root = settings.storage_path.resolve()
    hocr_path = output_dir / f"{leaf.leaf:04d}_hocr.html"
    lines_path = output_dir / f"{leaf.leaf:04d}_lines.json"
    atomic_write(hocr_path, page.html.encode("utf-8"))
    atomic_write(lines_path, json.dumps(check.lines, ensure_ascii=False).encode("utf-8"))
    outcome = HocrLeafOutcome(
        leaf=leaf.leaf,
        hocr=hocr_path.relative_to(root).as_posix(),
        text=page.text,
        mean_conf=page.mean_conf,
        lines=lines_path.relative_to(root).as_posix(),
        suspicious=check.suspicious,
    )
    return outcome, (lexicon.dictionaries if lexicon else [])


def read_page(
    image: Path,
    *,
    leaf: int,
    dpi: int,
    engine: Engine,
    language: str,
    languages: list[str],
    settings: Settings,
    line_check: bool,
    scratch: Path,
    deadline: float | None,
    ask: linecheck.Ask | None = None,
) -> tuple[hocr.Page, linecheck.PageCheck, linecheck.Lexicon | None]:
    """OCR one page image to hOCR and, with ``line_check``, check its lines (``ask``: the judge, ``judge()``).
    Shared with `files.ocr`."""
    timeout = None if deadline is None else max(deadline - time.monotonic(), 1.0)
    try:
        document = engine.recognize(image, languages, dpi, settings, timeout=timeout)
    except EngineError as error:
        if deadline is not None and time.monotonic() > deadline:
            raise DeadlineExceeded(str(error)) from error
        raise
    page = hocr.parse(document, leaf)
    if not line_check:
        return page, linecheck.PageCheck(lines=[linecheck.row(line) for line in page.lines]), None
    lexicon = linecheck.lexicon_for(languages, settings)
    check = linecheck.check_page(
        page.lines,
        engine=engine.name,
        languages=languages,
        lexicon=lexicon,
        second=_second_reader(image, dpi, engine, language, settings, scratch, deadline),
        settings=settings,
        ask=ask,
        before_each=lambda: _check_deadline(deadline),
    )
    return page, check, lexicon


#: The process's judge clients (their circuit breakers live as long as the process), for the settings they were
#: built from.
_judges: tuple[Settings, Judges] | None = None


def judge(settings: Settings) -> linecheck.Ask | None:
    """The ``decider`` of one job: a fresh ``Panel`` (the job's budget) over the process's client and breaker.
    None when judges are off or ``DECIDER_URL`` is unset."""
    global _judges
    if _judges is None or _judges[0] is not settings:
        _judges = (settings, Judges.from_settings(settings))
    judges = _judges[1]
    if judges.decider is None:
        return None
    panel = judges.panel()
    return lambda question, state: panel.ask(judges.decider, question, state=state)


def _second_reader(
    image: Path, dpi: int, engine: Engine, language: str, settings: Settings, scratch: Path, deadline: float | None
) -> linecheck.SecondReader | None:
    """The other enabled, installed engine that reads the language, reading one line crop; None if there is none."""
    other = second_engine(engine, settings)
    if other is None:
        return None
    try:
        languages = other.languages_for(language)
    except EngineError:
        logger.info("Line check: %s does not read %s; suspicious lines stay disputed", other.name, language)
        return None
    crop_path = scratch / f"line-{image.stem}.png"

    def read(box: Box) -> LineReading | None:
        with Image.open(image) as page:
            x0, y0, x1, y1 = box
            padded = (
                max(x0 - LINE_PADDING, 0),
                max(y0 - LINE_PADDING, 0),
                min(x1 + LINE_PADDING, page.width),
                min(y1 + LINE_PADDING, page.height),
            )
            if padded[2] <= padded[0] or padded[3] <= padded[1]:
                return None
            page.crop(padded).save(crop_path, dpi=(dpi, dpi))
        timeout = None if deadline is None else max(deadline - time.monotonic(), 1.0)
        try:
            document = other.recognize(crop_path, languages, dpi, settings, timeout=timeout, line=True)
        except EngineError:
            logger.warning("Line check: %s could not read a line crop", other.name, exc_info=True)
            return None
        text, conf = hocr.line_text(document)
        return LineReading(text=text, conf=conf, engine=other.name)

    return read


def second_engine(engine: Engine, settings: Settings) -> Engine | None:
    for name in settings.enabled_engines:
        other = ENGINES.get(name)
        if other is not None and other.name != engine.name and other.available():
            return other
    return None


def masked(source: Path, mask: list[Box], target: Path, dpi: int) -> Path:
    """``source`` with every box painted white, as a private copy at ``target``; ``source`` itself if no mask."""
    if not mask:
        return source
    with Image.open(source) as opened:
        if opened.mode.startswith("I") or opened.mode == "F":
            # 16-bit grey: scale to 8 bits (a plain convert("L") clips everything above 255 to white).
            image = opened.convert("I").point(lambda value: value / 256).convert("L")
        else:
            image = opened.convert("L" if opened.mode in ("1", "L", "LA") else "RGB")
    white = 255 if image.mode == "L" else (255, 255, 255)
    draw = ImageDraw.Draw(image)
    for x0, y0, x1, y1 in mask:
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, image.width), min(y1, image.height)
        if x1 > x0 and y1 > y0:
            draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=white)
    image.save(target, dpi=(dpi, dpi))
    return target


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise DeadlineExceeded("the hOCR run passed its deadline")


def _message(error: Exception, settings: Settings) -> str:
    if isinstance(error, DeadlineExceeded):
        return f"time limit exceeded ({settings.task_soft_time_limit} s); remaining leaves abandoned"
    if isinstance(error, (EngineError, FileNotFoundError, ValueError)):
        return str(error)
    return f"{type(error).__name__}: {error}"


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", name)[:60]
