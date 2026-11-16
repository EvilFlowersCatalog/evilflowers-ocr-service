"""Suspicious OCR lines (issue #8): flag likely errors, accept only corroborated corrections.

Tesseract invents punctuation on noisy scans; RapidOCR turns carons into apostrophes
(``t'``, ``l'``, ``d'`` for ť, ľ, ď; README benchmark). Neither may reach a published
book silently, and nothing here edits silently either.

Signals (no model). A line is suspicious when any of these fires:

- ``low_confidence_unknown_words``: mean word confidence under ``LINE_CHECK_CONFIDENCE``
  **and** a word outside the lexicon (hunspell, per language). Both, because a proper name
  is outside every lexicon and a clean low-confidence line is often right;
- ``broken_diacritics``: ``t'`` / ``l'`` / ``d'`` (and capitals) in a Slovak or Czech line.
  Confidence plays no part: RapidOCR is sure of its apostrophes;
- ``punctuation``: a run of punctuation that is not an ellipsis or ``?!``, or a word made
  of punctuation only;
- ``hyphenation``: a hyphen followed by a space inside a line (``slo- vo``), ``¬`` or a soft hyphen.

Second reading. The line's crop is read by the other engine (Tesseract ↔ RapidOCR, in line
mode). A correction is accepted only when both readings are the same text after
normalisation **and** they are at most ``max(2, LINE_CHECK_MAX_EDIT_RATIO × length)`` edits
apart (a broken diacritic ``t'`` counts as one symbol, so ``t'`` → ``ť`` is one edit).
``chosen`` is then the engine's reading, with broken diacritics repaired only where the
second reading has the caron: if both engines read ``t'`` (``it's``, ``d'Artagnan``), it stays
as read. Otherwise the line is ``disputed`` with every reading, and ``chosen`` is None. No
second engine (not enabled, not installed, does not read the language): disputed with the
one reading.

Normalisation, for the comparison only: Unicode NFC; ``’ ` ´`` → ``'``; broken diacritics →
carons; ``„ “ ” » «`` → ``"``; ``– —`` → ``-``; whitespace collapsed, and no space before
``. , ; : ! ?``. Case is kept.

Judge (optional). A disputed line with two readings is put to ``decider`` (Laya, text only;
the kit's ``judges``, evilflowers-worker#6): "which reading is the correct text of this
line?", answered ``a``, ``b`` or ``neither`` with a probability. ``p`` and ``model``
(``model@revision``) are stored on the line either way. The dispute is settled only when the
verdict falls in the calibrated ``keep`` band (``JUDGE_THRESHOLDS`` for ``ocr_line_reading``,
knowledge#65); without thresholds, or in the ``review`` band, the line stays disputed. A judge
that does not answer (off, unreachable, over budget) leaves the line as it was.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import cache

from evilflowers_worker.judges import Choice, Question, Verdict

from evilflowers_ocr_worker.contract import LineReading, SuspiciousLine
from evilflowers_ocr_worker.hocr import Line
from evilflowers_ocr_worker.settings import Settings

logger = logging.getLogger(__name__)

#: Languages whose lines are checked for ``t'`` / ``l'`` / ``d'``.
CARON_LANGUAGES = frozenset({"slk", "ces"})
#: The judge's question id (``JUDGE_THRESHOLDS`` key, the ``question`` label of ``ocr_judge_total``).
JUDGE_QUESTION = "ocr_line_reading"
_LANGUAGE_NAMES = {"slk": "Slovak", "ces": "Czech", "eng": "English", "deu": "German", "hun": "Hungarian"}

BROKEN_DIACRITICS = {"t'": "ť", "d'": "ď", "l'": "ľ", "T'": "Ť", "D'": "Ď", "L'": "Ľ"}
_APOSTROPHES = str.maketrans({"’": "'", "`": "'", "´": "'"})
_QUOTES = str.maketrans({"„": '"', "“": '"', "”": '"', "»": '"', "«": '"', "–": "-", "—": "-"})
#: t' / d' / l' standing for a caron: followed by a letter, a space, punctuation or the line's end.
_BROKEN = re.compile(r"[tdlTDL]'(?=[^\W\d_]|\s|$|[.,;:!?])")
_PUNCT_RUN = re.compile(r"[.,;:!?'\"`´’^~|*_•]{2,}")
_PUNCT_RUN_OK = re.compile(r"^(\.+|…|[!?]{2}|[.!?,;:]['\"’])$")
_PUNCT_TOKEN = re.compile(r"^[.,;:'\"`´’^~|_*]+$")
_HYPHENATION = re.compile(r"[^\W\d_]-\s+[^\W\d_]|[¬­]")
_WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")
_SPACE_BEFORE = re.compile(r"\s+([.,;:!?])")
_SYMBOL = re.compile(r"[tdlTDL]'(?=[^\W\d_]|\s|$|[.,;:!?])|.", re.DOTALL)


class Lexicon:
    """Words outside a set of hunspell dictionaries (``hunspell -l``), one process per call."""

    def __init__(self, dictionaries: Sequence[str], binary: str = "hunspell", timeout: float = 60.0):
        self.dictionaries = list(dictionaries)
        self.binary = binary
        self.timeout = timeout

    def unknown(self, words: Iterable[str]) -> set[str]:
        words = sorted(set(words))
        if not words or not self.dictionaries:
            return set()
        command = [self.binary, "-d", ",".join(self.dictionaries), "-l", "-i", "utf-8"]
        try:
            done = subprocess.run(
                command, input="\n".join(words), capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except (OSError, subprocess.SubprocessError):
            logger.warning("hunspell failed; no lexicon signal for this page", exc_info=True)
            return set()
        return {word for word in done.stdout.split() if word}


def lexicon_for(languages: Sequence[str], settings: Settings) -> Lexicon | None:
    """The installed dictionaries of ``languages``; None when there are none (the outcome says so)."""
    names = [settings.hunspell_dictionaries[code] for code in languages if code in settings.hunspell_dictionaries]
    installed = [name for name in dict.fromkeys(names) if _dictionary_installed(name)]
    return Lexicon(installed) if installed else None


@cache
def _dictionary_installed(name: str) -> bool:
    if shutil.which("hunspell") is None:
        return False
    try:
        done = subprocess.run(
            ["hunspell", "-d", name, "-l"], input="", capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and "Can't open" not in done.stderr


# -- signals --------------------------------------------------------------------------------------------------------


def words(text: str) -> list[str]:
    return [word for word in _WORD.findall(text) if len(word) > 1]


def signals(text: str, conf: float, unknown: set[str], *, threshold: float, carons: bool) -> list[str]:
    reasons = []
    if conf < threshold and any(word in unknown for word in words(text)):
        reasons.append("low_confidence_unknown_words")
    if carons and _BROKEN.search(text.translate(_APOSTROPHES)):
        reasons.append("broken_diacritics")
    runs = [run for run in _PUNCT_RUN.findall(text) if not _PUNCT_RUN_OK.match(run)]
    if runs or any(_PUNCT_TOKEN.match(token) for token in text.split()):
        reasons.append("punctuation")
    if _HYPHENATION.search(text):
        reasons.append("hyphenation")
    return reasons


# -- agreement ------------------------------------------------------------------------------------------------------


def repair(text: str) -> str:
    """NFC, broken diacritics → carons, whitespace collapsed: the only edit an agreement may make."""
    text = unicodedata.normalize("NFC", text).translate(_APOSTROPHES)
    text = _BROKEN.sub(lambda match: BROKEN_DIACRITICS[match[0]], text)
    return " ".join(text.split())


def canonical(text: str) -> str:
    """``repair`` plus quotes, dashes and the space before punctuation, for comparing two readings."""
    return _SPACE_BEFORE.sub(r"\1", repair(text).translate(_QUOTES))


def edit_distance(a: Sequence[str], b: Sequence[str]) -> int:
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


def symbols(text: str) -> list[str]:
    """The characters of a reading, a broken diacritic (``t'``) counted as one: ``t'`` → ``ť`` is one edit."""
    return _SYMBOL.findall(unicodedata.normalize("NFC", text).translate(_APOSTROPHES))


def agree(first: str, second: str, max_edit_ratio: float) -> bool:
    if canonical(first) != canonical(second):
        return False
    a, b = symbols(first), symbols(second)
    return edit_distance(a, b) <= max(2, round(max_edit_ratio * max(len(a), len(b))))


def corroborated(first: str, second: str) -> str:
    """The accepted text of two agreeing readings: ``first``, its carons repaired only if ``second`` has them.

    When the second reading has a ``t'`` too, both engines read an apostrophe (``it's``, ``d'Artagnan``) and nothing
    corroborates a caron, so the text stays as read.
    """
    if _BROKEN.search(unicodedata.normalize("NFC", second).translate(_APOSTROPHES)):
        return " ".join(first.split())
    return repair(first)


# -- the judge ------------------------------------------------------------------------------------------------------


def reading_question(readings: Sequence[LineReading], languages: Sequence[str]) -> tuple[Question, str]:
    """The ``decider`` question for a disputed line, and its state."""
    language = " or ".join(_LANGUAGE_NAMES.get(code, code) for code in languages)
    question = Choice(
        id=JUDGE_QUESTION,
        instructions=f"Two OCR engines read the same printed line of a {language} book differently. "
        "Which reading is the correct text of the line?",
        criteria={
            "a": "reading a is the correct text",
            "b": "reading b is the correct text",
            "neither": "neither reading is a plausible line of text",
        },
        reject=frozenset({"neither"}),
    )
    return question, f"Reading a: {readings[0].text}\nReading b: {readings[1].text}"


def judged(flagged: SuspiciousLine, verdict: Verdict | None) -> SuspiciousLine:
    """The line with the judge's verdict recorded; settled only when the verdict picks a reading in the keep band."""
    if verdict is None:
        return flagged
    update: dict = {"p": verdict.p, "model": verdict.model_ref, "verdict": verdict.verdict, "band": verdict.band}
    if verdict.band == "keep" and verdict.verdict in ("a", "b"):
        update |= {"chosen": flagged.readings["ab".index(verdict.verdict)].text, "disputed": False}
    return flagged.model_copy(update=update)


#: Asks the judge one question with a state; None when it does not answer (``Panel.ask`` bound to ``decider``).
Ask = Callable[[Question, str], Verdict | None]


# -- the check ------------------------------------------------------------------------------------------------------

#: Reads one line crop with the second engine: (line box) → its reading, or None when it cannot.
SecondReader = Callable[[tuple[int, int, int, int]], LineReading | None]


@dataclass
class PageCheck:
    """Every line with its verdict (``NNNN_lines.json``) and the suspicious ones (the outcome)."""

    lines: list[dict] = field(default_factory=list)
    suspicious: list[SuspiciousLine] = field(default_factory=list)


def check_page(
    lines: Sequence[Line],
    *,
    engine: str,
    languages: Sequence[str],
    lexicon: Lexicon | None,
    second: SecondReader | None,
    settings: Settings,
    ask: Ask | None = None,
    before_each: Callable[[], None] = lambda: None,
) -> PageCheck:
    """Check the lines of one page. ``before_each`` runs before every second reading and judge call (the run's
    deadline); ``ask`` is the judge, None without one."""
    unknown = lexicon.unknown(word for line in lines for word in words(line.text)) if lexicon else set()
    carons = bool(CARON_LANGUAGES & set(languages))
    result = PageCheck()
    budget = settings.line_check_max_lines
    for line in lines:
        reasons = signals(line.text, line.conf, unknown, threshold=settings.line_check_confidence, carons=carons)
        entry = {**row(line), "suspicious": False}
        if reasons:
            readings = [LineReading(text=line.text, conf=line.conf, engine=engine)]
            if second is not None and budget > 0:
                budget -= 1
                before_each()
                if (other := second(line.bbox)) is not None:
                    readings.append(other)
            accepted = len(readings) == 2 and agree(
                readings[0].text, readings[1].text, settings.line_check_max_edit_ratio
            )
            flagged = SuspiciousLine(
                line=line.id,
                bbox=line.bbox,
                reasons=reasons,
                readings=readings,
                chosen=corroborated(readings[0].text, readings[1].text) if accepted else None,
                disputed=not accepted,
            )
            if flagged.disputed and len(readings) == 2 and ask is not None:
                before_each()
                flagged = judged(flagged, ask(*reading_question(readings, languages)))
            result.suspicious.append(flagged)
            entry.update(
                flagged.model_dump(
                    include={"suspicious", "reasons", "chosen", "disputed", "p", "model", "verdict", "band"}
                )
            )
            entry["readings"] = [reading.model_dump() for reading in readings]
        result.lines.append(entry)
    return result


def row(line: Line) -> dict:
    """A line as ``NNNN_lines.json`` lists it."""
    return {"line": line.id, "bbox": list(line.bbox), "text": line.text, "conf": line.conf}


def summary(checks: Iterable[tuple[int, SuspiciousLine]]) -> list[str]:
    """Warning lines for `files.ocr` (page number, 1-based): one per disputed or corrected line."""
    out = []
    for page, line in checks:
        reasons = ", ".join(line.reasons)
        if line.disputed:
            texts = " | ".join(f"{reading.engine}: {reading.text!r}" for reading in line.readings)
            out.append(f"page {page}: suspicious line ({reasons}), disputed: {texts}")
        elif line.chosen != line.readings[0].text:
            out.append(f"page {page}: suspicious line ({reasons}), both engines read {line.chosen!r}")
    return out
