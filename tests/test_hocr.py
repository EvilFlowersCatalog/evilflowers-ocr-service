"""hOCR of one leaf: the page relabelled for the Internet Archive, its text and confidence."""

from evilflowers_ocr_worker import hocr

TESSERACT = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title></title></head><body>
<div class='ocr_page' id='page_1' title='image "x.png"; bbox 0 0 1748 2480; ppageno 0'>
 <div class='ocr_carea' id='block_1_1' title="bbox 180 300 900 420">
  <p class='ocr_par' id='par_1_1'>
   <span class='ocr_line' id='line_1_1' title="bbox 180 300 900 350; baseline 0 -10">
    <span class='ocrx_word' id='word_1_1' title='bbox 180 300 300 350; x_wconf 96'>Ťažký</span>
    <span class='ocrx_word' id='word_1_2' title='bbox 320 300 500 350; x_wconf 90'>ľadový</span>
   </span>
   <span class='ocr_header' id='line_1_2' title="bbox 180 370 600 420">
    <span class='ocrx_word' id='word_1_3' title='bbox 180 370 300 420; x_wconf 60'>čaj</span>
    <span class='ocrx_word' id='word_1_4' title='bbox 320 370 330 420; x_wconf 95'> </span>
   </span>
  </p>
 </div>
</div></body></html>"""


def test_the_page_is_relabelled_for_the_leaf():
    page = hocr.parse(TESSERACT, 3)

    assert 'id="page_000003"' in page.html or "id='page_000003'" in page.html
    assert "line_000003_1" in page.html and "word_000003_4" in page.html
    assert "page_1" not in page.html and "line_1_" not in page.html
    assert "ppageno 3" in page.html
    assert page.html.lstrip().startswith("<div")  # one ocr_page, not a document


def test_relabelling_is_idempotent():
    once = hocr.parse(TESSERACT, 3)
    twice = hocr.parse(once.html, 3)

    assert twice.html == once.html


def test_text_lines_and_confidence():
    page = hocr.parse(TESSERACT, 3)

    assert page.text == "Ťažký ľadový\nčaj"  # a header is a line too; an empty word is no word
    assert [line.id for line in page.lines] == ["line_000003_1", "line_000003_2"]
    assert page.lines[0].bbox == (180, 300, 900, 350)
    assert page.lines[0].conf == 93.0
    assert page.mean_conf == round((96 + 90 + 60) / 3, 2)


def test_an_empty_document_is_an_empty_page():
    page = hocr.parse("", 7)

    assert page.text == "" and page.mean_conf == 0.0 and page.lines == []
    assert "page_000007" in page.html


def test_line_text_of_a_crop():
    assert hocr.line_text(TESSERACT) == ("Ťažký ľadový čaj", round((96 + 90 + 60) / 3, 2))
