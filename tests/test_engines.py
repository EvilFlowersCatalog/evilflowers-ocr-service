import pytest

from conftest import needs_tesseract
from evilflowers_ocr_worker import __main__ as cli
from evilflowers_ocr_worker.engines import ENGINES, EngineError, RapidOcrEngine, get_engine, normalise_language

REQUIRED = {"slk", "ces", "eng", "deu", "hun", "pol", "rus", "ukr", "fra"}


@pytest.mark.parametrize(("given", "expected"), [("slk", "slk"), ("SLO", "slk"), ("cze", "ces"), ("ger", "deu")])
def test_alpha3_aliases(given, expected):
    assert normalise_language(given) == expected


def test_default_engine(settings):
    assert get_engine(None, settings).name == "tesseract"


def test_engine_names_are_case_insensitive(settings):
    assert get_engine(" Tesseract ", settings).name == "tesseract"


def test_unknown_engine(settings):
    with pytest.raises(EngineError, match="unknown"):
        get_engine("abbyy", settings)


@needs_tesseract
def test_language_not_installed(settings):
    with pytest.raises(EngineError, match="cannot read"):
        ENGINES["tesseract"].language("xxq")


@pytest.mark.skipif(not RapidOcrEngine().available(), reason="rapidocr is not installed")
def test_rapidocr_reads_the_required_languages():
    assert RapidOcrEngine().languages() >= REQUIRED


def test_cli_lists_engines(capsys):
    assert cli.main(["engines"]) == 0
    out = capsys.readouterr().out
    assert '"tesseract"' in out
    assert '"rapidocr"' in out
