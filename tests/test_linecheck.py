"""Suspicious OCR lines (issue #8): the signals, the normalisation and the agreement rule."""

import pytest
from evilflowers_worker.judges import Verdict

from evilflowers_ocr_worker import linecheck
from evilflowers_ocr_worker.contract import LineReading
from evilflowers_ocr_worker.hocr import Line, Word


def line(text: str, conf: float = 95.0, id_: str = "line_000001_1") -> Line:
    return Line(id_, (10, 20, 300, 60), tuple(Word(word, conf) for word in text.split()))


def signals(text, conf=95.0, unknown=(), carons=True):
    return linecheck.signals(text, conf, set(unknown), threshold=80.0, carons=carons)


# -- signals --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["t'ažký ľadový čaj", "Kôn' ... l'adový", "D'alšie úlohy", "zjest’ jablko"])
def test_rapidocr_caron_apostrophes_are_broken_diacritics(text):
    assert "broken_diacritics" in signals(text)


def test_broken_diacritics_need_a_slovak_or_czech_line():
    assert signals("it's what we don't want", carons=False) == []


def test_broken_diacritics_fire_at_any_confidence():
    """RapidOCR is sure of its apostrophes: the signal must not wait for low confidence."""
    assert "broken_diacritics" in signals("t'ažký", conf=99.0)


@pytest.mark.parametrize("text", ["Kôň ,, skákal", "dlhý múr .;", "a ' b", "slovo ~~ iné"])
def test_invented_punctuation(text):
    assert "punctuation" in signals(text)


@pytest.mark.parametrize("text", ["A potom...", "Naozaj?!", 'Povedal: „Áno."', "Kôň, mačka; pes."])
def test_ordinary_punctuation_is_fine(text):
    assert "punctuation" not in signals(text)


@pytest.mark.parametrize("text", ["slo- vo v strede", "roz¬delené", "roz­delené"])
def test_hyphenation_artefacts(text):
    assert "hyphenation" in signals(text)


def test_a_hyphen_at_the_line_end_is_hyphenation_not_an_artefact():
    assert signals("na konci riadku je roz-") == []


def test_low_confidence_needs_an_unknown_word_too():
    assert signals("Ťažký ľadový čaj", conf=50.0) == []
    assert signals("Ťažký ľadový čaj", conf=95.0, unknown={"ľadový"}) == []
    assert signals("Ťažký ľadový čaj", conf=50.0, unknown={"ľadový"}) == ["low_confidence_unknown_words"]


# -- normalisation and agreement ------------------------------------------------------------------------------------


def test_repair_turns_broken_diacritics_into_carons():
    assert linecheck.repair("t'ažký  l'adový D'alej") == "ťažký ľadový Ďalej"
    assert linecheck.repair("zjest’ jablko") == "zjesť jablko"


def test_canonical_unifies_quotes_dashes_and_spacing():
    assert linecheck.canonical("„Áno“ – povedal .") == linecheck.canonical('"Áno" - povedal.')


@pytest.mark.parametrize(
    ("first", "second", "agree"),
    [
        ("t'ažký l'adový čaj", "ťažký ľadový čaj", True),  # RapidOCR vs Tesseract: the caron repaired
        ("Ťažký ľadový čaj", "Ťažký ľadový čaj", True),
        ("Ťažký ľadový čaj", "Ťažký ľadový čal", False),  # a real disagreement is never "corrected"
        ("Ťažký ľadový čaj ,.", "Ťažký ľadový čaj", False),
        ("Kôň", "Koň", False),
    ],
)
def test_agreement(first, second, agree):
    assert linecheck.agree(first, second, 0.15) is agree


def test_agreement_needs_a_small_edit_distance():
    # Same after normalisation, but every word's caron was broken: too many edits for a short line.
    assert not linecheck.agree("t' d' l' t' d'", "ť ď ľ ť ď", 0.15)
    assert linecheck.edit_distance("kitten", "sitting") == 3
    assert linecheck.symbols("t'ava") == ["t'", "a", "v", "a"]


# -- the check ------------------------------------------------------------------------------------------------------


def check(lines, second, settings, lexicon=None):
    return linecheck.check_page(
        lines, engine="rapidocr", languages=["slk"], lexicon=lexicon, second=second, settings=settings
    )


def test_a_corroborated_correction_is_accepted(settings):
    second = lambda _box: LineReading(text="Ťažký ľadový čaj", conf=91.0, engine="tesseract")  # noqa: E731

    result = check([line("T'ažký l'adový čaj")], second, settings)

    [flagged] = result.suspicious
    assert flagged.reasons == ["broken_diacritics"]
    assert flagged.chosen == "Ťažký ľadový čaj"
    assert not flagged.disputed
    assert [reading.engine for reading in flagged.readings] == ["rapidocr", "tesseract"]
    assert flagged.p is None and flagged.model is None


def test_an_apostrophe_both_engines_read_is_never_turned_into_a_caron(settings):
    """`it's` / `d'Artagnan` in a slk+eng line: flagged, corroborated, and kept exactly as read."""
    second = lambda _box: LineReading(text="d'Artagnan it's here", conf=93.0, engine="tesseract")  # noqa: E731

    [flagged] = check([line("d'Artagnan it's here")], second, settings).suspicious

    assert flagged.reasons == ["broken_diacritics"]
    assert not flagged.disputed
    assert flagged.chosen == "d'Artagnan it's here"


def test_a_disagreement_is_disputed_with_both_readings(settings):
    second = lambda _box: LineReading(text="Ťažký ľadový čas", conf=70.0, engine="tesseract")  # noqa: E731

    [flagged] = check([line("T'ažký l'adový čaj")], second, settings).suspicious

    assert flagged.disputed and flagged.chosen is None
    assert [reading.text for reading in flagged.readings] == ["T'ažký l'adový čaj", "Ťažký ľadový čas"]


def verdict(answer: str, p: float, band=None) -> Verdict:
    return Verdict(question=linecheck.JUDGE_QUESTION, verdict=answer, p=p, model="laya", revision="r1", band=band)


def disputed_check(settings, answer):
    asked = []

    def ask(question, state):
        asked.append((question, state))
        return answer

    second = lambda _box: LineReading(text="Ťažký ľadový čas", conf=70.0, engine="tesseract")  # noqa: E731
    [flagged] = linecheck.check_page(
        [line("T'ažký l'adový čaj")],
        engine="rapidocr",
        languages=["slk"],
        lexicon=None,
        second=second,
        settings=settings,
        ask=ask,
    ).suspicious
    return flagged, asked


def test_the_judge_settles_a_dispute_only_in_the_keep_band(settings):
    flagged, asked = disputed_check(settings, verdict("b", 0.93, band="keep"))

    [(question, state)] = asked
    assert question.id == "ocr_line_reading" and "Slovak" in question.instructions
    assert state == "Reading a: T'ažký l'adový čaj\nReading b: Ťažký ľadový čas"
    assert not flagged.disputed and flagged.chosen == "Ťažký ľadový čas"
    assert (flagged.p, flagged.model, flagged.verdict, flagged.band) == (0.93, "laya@r1", "b", "keep")


@pytest.mark.parametrize("band", [None, "review"])
def test_without_calibration_the_judge_only_records_its_answer(settings, band):
    flagged, _asked = disputed_check(settings, verdict("a", 0.97, band=band))

    assert flagged.disputed and flagged.chosen is None
    assert (flagged.p, flagged.model, flagged.verdict) == (0.97, "laya@r1", "a")


def test_neither_reading_keeps_the_dispute(settings):
    flagged, _asked = disputed_check(settings, verdict("neither", 0.9, band="hide"))

    assert flagged.disputed and flagged.chosen is None and flagged.band == "hide"


def test_a_judge_that_does_not_answer_changes_nothing(settings):
    flagged, asked = disputed_check(settings, None)

    assert len(asked) == 1
    assert flagged.disputed and flagged.p is None and flagged.model is None


def test_the_judge_is_not_asked_about_agreeing_readings(settings):
    second = lambda _box: LineReading(text="Ťažký", conf=90.0, engine="tesseract")  # noqa: E731
    ask = lambda *_a: pytest.fail("agreeing readings need no judge")  # noqa: E731

    linecheck.check_page(
        [line("T'ažký")],
        engine="rapidocr",
        languages=["slk"],
        lexicon=None,
        second=second,
        settings=settings,
        ask=ask,
    )


def test_no_judge_is_built_without_a_decider_url(settings):
    from evilflowers_ocr_worker import standalone

    assert standalone.judge(settings) is None
    on = settings.model_copy(update={"judges_enabled": True, "decider_url": "http://decider.invalid"})
    assert callable(standalone.judge(on))


def test_without_a_second_engine_the_line_is_disputed(settings):
    [flagged] = check([line("T'ažký")], None, settings).suspicious

    assert flagged.disputed and len(flagged.readings) == 1


def test_clean_lines_are_not_read_again(settings):
    second = lambda _box: pytest.fail("a clean line must not be read again")  # noqa: E731

    result = check([line("Ťažký ľadový čaj"), line("Kôň skákal.", id_="line_000001_2")], second, settings)

    assert result.suspicious == []
    assert [row["suspicious"] for row in result.lines] == [False, False]
    assert result.lines[0] == {
        "line": "line_000001_1",
        "bbox": [10, 20, 300, 60],
        "text": "Ťažký ľadový čaj",
        "conf": 95.0,
        "suspicious": False,
    }


def test_second_readings_are_capped_per_page(settings):
    settings = settings.model_copy(update={"line_check_max_lines": 1})
    reads = []

    def second(box):
        reads.append(box)
        return LineReading(text="ť", conf=90.0, engine="tesseract")

    result = check([line("t'", id_=f"line_000001_{n}") for n in range(3)], second, settings)

    assert len(reads) == 1
    assert len(result.suspicious) == 3
    assert [len(flagged.readings) for flagged in result.suspicious] == [2, 1, 1]


class FakeLexicon:
    def __init__(self, unknown):
        self.dictionaries = ["sk_SK"]
        self._unknown = set(unknown)
        self.calls = 0

    def unknown(self, words):
        self.calls += 1
        return self._unknown & set(words)


def test_the_lexicon_is_asked_once_per_page(settings):
    lexicon = FakeLexicon({"ľadoví"})

    result = check(
        [line("Ťažký ľadoví", conf=40.0), line("čaj", conf=40.0, id_="line_000001_2")], None, settings, lexicon=lexicon
    )

    assert lexicon.calls == 1
    assert [flagged.reasons for flagged in result.suspicious] == [["low_confidence_unknown_words"]]


def test_summary_names_disputed_and_corrected_lines():
    corrected = linecheck.SuspiciousLine(
        line="l1",
        bbox=(0, 0, 1, 1),
        reasons=["broken_diacritics"],
        disputed=False,
        chosen="ťava",
        readings=[
            LineReading(text="t'ava", conf=99, engine="rapidocr"),
            LineReading(text="ťava", conf=90, engine="tesseract"),
        ],
    )
    disputed = corrected.model_copy(update={"disputed": True, "chosen": None})

    lines = linecheck.summary([(2, corrected), (3, disputed)])

    assert lines[0] == "page 2: suspicious line (broken_diacritics), both engines read 'ťava'"
    assert lines[1].startswith('page 3: suspicious line (broken_diacritics), disputed: rapidocr: "t\'ava"')


@pytest.mark.skipif(not linecheck._dictionary_installed("sk_SK"), reason="hunspell sk_SK is not installed")
def test_hunspell_knows_slovak():
    lexicon = linecheck.Lexicon(["sk_SK"])

    assert lexicon.unknown(["ľadový", "čaj", "xqzvwk"]) == {"xqzvwk"}
