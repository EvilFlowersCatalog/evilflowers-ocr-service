"""hOCR of one leaf: the ``ocr_page`` an engine wrote, relabelled for the Internet Archive, with its lines.

Both engines write a whole document whose page is ``page_1`` (``line_1_3``, ``word_1_17`` …). The
books digitalizer keys pages on ``page_%06d`` (the leaf number, the IA convention) and assembles
the book's ``_hocr.html`` from the per-leaf fragments, so every id becomes leaf-unique:
``page_000003``, ``line_000003_3``. Relabelling is idempotent, so the digitalizer may apply its own
again.

Text and confidence follow the digitalizer's in-process stage: one text line per OCR line, and
the mean ``x_wconf`` over words (0..100). Tesseract writes lines as ``ocr_line``, ``ocr_header``,
``ocr_caption`` or ``ocr_textfloat``; all four are lines here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree
from lxml import html as lhtml

LINE_CLASSES = ("ocr_line", "ocr_header", "ocr_caption", "ocr_textfloat")

_ID_NUMBER = re.compile(r"^([a-z]+)_\d+")
_PPAGENO = re.compile(r"ppageno \d+")
_BBOX = re.compile(r"bbox (-?\d+) (-?\d+) (-?\d+) (-?\d+)")
_WCONF = re.compile(r"x_wconf (-?[\d.]+)")


@dataclass(frozen=True)
class Word:
    text: str
    conf: float


@dataclass(frozen=True)
class Line:
    id: str
    bbox: tuple[int, int, int, int]
    words: tuple[Word, ...]

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

    @property
    def conf(self) -> float:
        return _mean(word.conf for word in self.words)


@dataclass(frozen=True)
class Page:
    #: The ``ocr_page`` element, relabelled, as HTML.
    html: str
    lines: list[Line] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def mean_conf(self) -> float:
        return _mean(word.conf for line in self.lines for word in line.words)


def parse(document: str, leaf: int) -> Page:
    """The first ``ocr_page`` of ``document``, relabelled for ``leaf``. A document without one is an empty page."""
    if not document.strip():
        return Page(html=_empty(leaf))
    root = lhtml.fromstring(document.encode("utf-8"), parser=lhtml.HTMLParser(encoding="utf-8"))
    pages = root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' ocr_page ')]")
    if not pages:
        return Page(html=_empty(leaf))
    page = pages[0]
    _relabel(page, leaf)
    return Page(html=etree.tostring(page, method="html", encoding="unicode").strip(), lines=_lines(page))


def line_text(document: str) -> tuple[str, float]:
    """Text and mean confidence of a document that holds one line (a line crop's second reading)."""
    page = parse(document, 0)
    return " ".join(line.text for line in page.lines).strip(), page.mean_conf


def _lines(page) -> list[Line]:
    lines = []
    for element in page.iter(etree.Element):
        if element.get("class") not in LINE_CLASSES:
            continue
        words = tuple(
            Word(text, _conf(word.get("title", "")))
            for word in element.iter(etree.Element)
            if word.get("class") == "ocrx_word" and (text := word.text_content().strip())
        )
        if words:
            lines.append(Line(element.get("id", ""), _bbox(element.get("title", "")), words))
    return lines


def _relabel(page, leaf: int) -> None:
    for element in page.iter(etree.Element):
        if element_id := element.get("id"):
            element.set("id", _ID_NUMBER.sub(lambda match: f"{match[1]}_{leaf:06d}", element_id))
    page.set("id", f"page_{leaf:06d}")
    title = page.get("title", "")
    if "ppageno" in title:
        page.set("title", _PPAGENO.sub(f"ppageno {leaf}", title))
    else:
        page.set("title", f"{title}; ppageno {leaf}".lstrip("; "))


def _empty(leaf: int) -> str:
    return f"<div class='ocr_page' id='page_{leaf:06d}' title='ppageno {leaf}'></div>"


def _bbox(title: str) -> tuple[int, int, int, int]:
    match = _BBOX.search(title)
    return (int(match[1]), int(match[2]), int(match[3]), int(match[4])) if match else (0, 0, 0, 0)


def _conf(title: str) -> float:
    match = _WCONF.search(title)
    return float(match[1]) if match else 0.0


def _mean(values) -> float:
    values = list(values)
    return round(sum(values) / len(values), 2) if values else 0.0
