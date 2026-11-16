"""``evilflowers_ocr_worker.standalone.hocr`` (issue #7): leaf images → hOCR, outcome last."""

import hashlib
import json
from pathlib import Path

import pytest
from evilflowers_worker.testing import assert_outcome
from PIL import Image

from conftest import TOKEN, needs_slovak, needs_tesseract, slovak_leaf
from evilflowers_ocr_worker import standalone
from evilflowers_ocr_worker.contract import HocrRequest
from evilflowers_ocr_worker.engines import RapidOcrEngine

ITEM = "digitized/studio/stu/item-1"
JOB_DIR = f"{ITEM}/_studio/jobs/17-a1"
OUTCOME = f"{JOB_DIR}/outcome.json"


def message(**overrides) -> dict:
    base = {
        "run_id": "42",
        "job": "analyze-hocr-0003-a1",
        "token": TOKEN,
        "output_dir": JOB_DIR,
        "outcome": OUTCOME,
        "leaves": [{"leaf": 3, "image": f"{ITEM}/leaves/0003.png", "dpi": 300, "language": "slk+eng"}],
    }
    base.update(overrides)
    return base


@pytest.fixture
def leaf(storage) -> Path:
    return slovak_leaf(storage / ITEM / "leaves" / "0003.png")


def run(settings, **overrides) -> dict:
    return standalone.run(HocrRequest.model_validate(message(**overrides)), settings)


@pytest.mark.ocr
@needs_slovak
def test_a_slovak_page_becomes_hocr_with_its_diacritics(settings, storage, leaf):
    result = run(settings)

    hocr_file = f"{JOB_DIR}/0003_hocr.html"
    outcome = assert_outcome(storage, OUTCOME, run_id="42", token=TOKEN, status="done", after=hocr_file)
    assert result == {"job": "analyze-hocr-0003-a1", "status": "done", "leaves": 1, "suspicious": result["suspicious"]}
    [page] = outcome["leaves"]
    assert page["leaf"] == 3
    assert page["hocr"] == hocr_file
    assert page["lines"] == f"{JOB_DIR}/0003_lines.json"
    for word in ("Ťažký", "ľadový", "Ďalšie", "Žofia", "Kôň", "múr"):
        assert word in page["text"], word
    assert 0 < page["mean_conf"] <= 100
    html = (storage / hocr_file).read_text(encoding="utf-8")
    assert "page_000003" in html and "Ťažký" in html and "x_wconf" in html
    lines = json.loads((storage / page["lines"]).read_text(encoding="utf-8"))
    assert len(lines) >= 3 and all(0 <= row["conf"] <= 100 for row in lines)
    assert outcome["engine"] == "tesseract" and outcome["engine_version"]


@pytest.mark.ocr
@needs_slovak
def test_the_outcome_is_written_after_every_hocr(settings, storage, leaf):
    second = slovak_leaf(storage / ITEM / "leaves" / "0004.png", lines=["Druhá strana knihy."])
    leaves = [
        {"leaf": 3, "image": f"{ITEM}/leaves/0003.png", "language": "slk"},
        {"leaf": 4, "image": second.relative_to(storage).as_posix(), "language": "slk"},
    ]

    run(settings, leaves=leaves)

    for number in (3, 4):
        assert_outcome(storage, OUTCOME, status="done", after=f"{JOB_DIR}/{number:04d}_hocr.html")
        assert_outcome(storage, OUTCOME, after=f"{JOB_DIR}/{number:04d}_lines.json")


@pytest.mark.ocr
@needs_slovak
def test_the_mask_is_painted_on_a_private_copy(settings, storage, leaf):
    before = hashlib.sha256(leaf.read_bytes()).hexdigest()
    # The first line, "Ťažký ľadový čaj stál na stole.", at y 300..360.
    mask = [[0, 280, 1748, 380]]

    run(settings, leaves=[{"leaf": 3, "image": f"{ITEM}/leaves/0003.png", "language": "slk", "mask": mask}])

    text = assert_outcome(storage, OUTCOME, status="done")["leaves"][0]["text"]
    assert "stole" not in text and "Ťažký" not in text
    assert "Žofia" in text
    assert hashlib.sha256(leaf.read_bytes()).hexdigest() == before  # the shared image is never written


def test_mask_boxes_exclude_their_far_edge(tmp_path):
    source = tmp_path / "black.png"
    Image.new("L", (10, 10), 0).save(source)

    target = standalone.masked(source, [(2, 2, 5, 5), (-3, 8, 99, 99)], tmp_path / "masked.png", 300)

    with Image.open(target) as image:
        assert image.getpixel((2, 2)) == 255 and image.getpixel((4, 4)) == 255
        assert image.getpixel((5, 5)) == 0  # x1, y1 exclusive
        assert image.getpixel((0, 9)) == 255  # clipped to the page
    assert standalone.masked(source, [], tmp_path / "unused.png", 300) == source


def test_16_bit_scans_are_masked_without_washing_out(tmp_path):
    source = tmp_path / "grey16.png"
    Image.new("I;16", (4, 4), 20000).save(source)

    with Image.open(standalone.masked(source, [(0, 0, 1, 1)], tmp_path / "m.png", 300)) as image:
        assert image.getpixel((0, 0)) == 255
        assert image.getpixel((3, 3)) == 20000 // 256


@needs_slovak
def test_a_missing_image_writes_failed_then_raises(settings, storage):
    with pytest.raises(standalone.StandaloneFailed, match="does not exist"):
        run(settings)

    outcome = assert_outcome(storage, OUTCOME, run_id="42", token=TOKEN, status="failed")
    assert "does not exist" in outcome["error"] and outcome["leaves"] == []


@needs_slovak
def test_an_unread_language_fails_and_never_falls_back_to_english(settings, storage, leaf):
    leaves = [{"leaf": 3, "image": f"{ITEM}/leaves/0003.png", "language": "slk+xxx"}]

    with pytest.raises(standalone.StandaloneFailed, match="cannot read language 'xxx'"):
        run(settings, leaves=leaves)

    assert_outcome(storage, OUTCOME, status="failed")


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.json"])
def test_a_bad_outcome_path_is_refused_before_ocr(settings, path, monkeypatch):
    monkeypatch.setattr(standalone, "_process", lambda *_a: pytest.fail("OCR must not start"))

    with pytest.raises(standalone.InvalidRequest):
        run(settings, outcome=path)


@needs_tesseract
def test_an_output_dir_outside_the_storage_fails(settings, storage, leaf):
    with pytest.raises(standalone.StandaloneFailed, match="escapes STORAGE_PATH"):
        run(settings, output_dir="../elsewhere")

    assert_outcome(storage, OUTCOME, status="failed")


@needs_tesseract
def test_past_the_deadline_the_job_fails_with_a_timeout(settings, storage, leaf):
    settings = settings.model_copy(update={"task_soft_time_limit": -1})

    with pytest.raises(standalone.StandaloneFailed, match="time limit exceeded"):
        run(settings)

    assert_outcome(storage, OUTCOME, status="failed")


@needs_tesseract
def test_a_stopped_worker_writes_no_outcome(settings, storage, leaf, monkeypatch):
    def stopped(*_args, **_kwargs):
        raise SystemExit("terminated")

    monkeypatch.setattr(standalone, "read_page", stopped)

    with pytest.raises(SystemExit):
        run(settings)

    assert not (storage / OUTCOME).exists()


def test_the_message_is_the_kits_standalone_job_and_inputs_come_from_the_leaves():
    from evilflowers_worker.contract import StandaloneJob

    request = HocrRequest.model_validate(message())

    assert isinstance(request, StandaloneJob)
    assert request.inputs == [f"{ITEM}/leaves/0003.png"]
    explicit = HocrRequest.model_validate(message(inputs=["digitized/other.tif"]))
    assert explicit.inputs == ["digitized/other.tif"]


def test_the_token_is_not_in_the_result_or_the_logs(settings, storage, caplog):
    with pytest.raises(standalone.StandaloneFailed):
        run(settings)

    assert TOKEN not in caplog.text
    assert TOKEN not in repr(HocrRequest.model_validate(message()))


@pytest.mark.ocr
@pytest.mark.skipif(not RapidOcrEngine().available(), reason="rapidocr is not installed")
def test_rapidocr_reads_a_leaf_to_hocr(settings, storage, leaf):
    try:
        run(settings, engine="rapidocr", line_check=False)
    except standalone.StandaloneFailed as error:
        if "model" in str(error):
            pytest.skip(f"RapidOCR models are not available here: {error}")
        raise

    outcome = assert_outcome(storage, OUTCOME, status="done", after=f"{JOB_DIR}/0003_hocr.html")
    assert outcome["engine"] == "rapidocr"
    assert "Bratislave" in outcome["leaves"][0]["text"]
    assert "page_000003" in (storage / JOB_DIR / "0003_hocr.html").read_text(encoding="utf-8")


# -- Through the task (`runtime.run_task`) -----------------------------------------------------------------------


@pytest.fixture
def task(settings, monkeypatch):
    from evilflowers_ocr_worker import tasks

    monkeypatch.setattr(standalone, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    return tasks.standalone_hocr


def test_the_task_is_registered_under_its_contract_name():
    from evilflowers_ocr_worker.worker import app

    app.loader.import_default_modules()
    assert "evilflowers_ocr_worker.standalone.hocr" in app.tasks
    assert "evilflowers_ocr_worker.ocr" in app.tasks  # files.ocr stays


def test_task_options_keep_late_ack_and_the_hard_limit(task, settings):
    assert task.acks_late and task.reject_on_worker_lost
    assert task.time_limit == settings.task_time_limit


@needs_slovak
def test_the_task_returns_a_compact_result(task, storage, leaf):
    result = task.apply(kwargs=message(), task_id="t-1", throw=True).get()

    assert result["status"] == "done" and result["leaves"] == 1
    assert len(repr(result)) < 200
    assert_outcome(storage, OUTCOME, status="done")


def test_an_invalid_message_with_an_outcome_says_why(task, storage):
    with pytest.raises(standalone.InvalidRequest):
        task.apply(kwargs=message(leaves=[{"leaf": "three"}]), task_id="t-1", throw=True)

    outcome = assert_outcome(storage, OUTCOME, run_id="42", token=TOKEN, status="failed")
    assert "standalone.hocr contract" in outcome["error"]
    assert TOKEN not in outcome["error"]


def test_an_invalid_message_without_an_outcome_fails_at_once(task, storage):
    args = message()
    del args["outcome"]

    with pytest.raises(standalone.InvalidRequest):
        task.apply(kwargs=args, task_id="t-1", throw=True)


def test_past_the_delivery_cap_a_failed_outcome_is_written(task, settings, storage, monkeypatch):
    from evilflowers_worker import runtime

    class Capped:
        def record(self, _task_id, *, redelivered):
            return settings.max_deliveries + 1

        def forget(self, _task_ids):
            pass

    monkeypatch.setattr(runtime, "_runtime", runtime.Runtime("ocr", settings, Capped()))
    monkeypatch.setattr(standalone, "_process", lambda *_a: pytest.fail("past the cap nothing is OCR'd"))

    with pytest.raises(standalone.GaveUp):
        task.apply(kwargs=message(), task_id="t-1", throw=True)

    assert "gave up" in assert_outcome(storage, OUTCOME, status="failed")["error"]


# -- The line check in both tasks (issue #8) ----------------------------------------------------------------------


@pytest.mark.ocr
@needs_slovak
def test_suspicious_lines_reach_the_outcome(settings, storage):
    slovak_leaf(storage / ITEM / "leaves" / "0003.png", lines=["Ťažký ľadový čaj ,,;; stál na stole."])

    run(settings, leaves=[{"leaf": 3, "image": f"{ITEM}/leaves/0003.png", "language": "slk"}])

    outcome = assert_outcome(storage, OUTCOME, status="done")
    flagged = next(line for line in outcome["leaves"][0]["suspicious"] if "punctuation" in line["reasons"])
    assert flagged["readings"][0]["engine"] == "tesseract"
    assert set(flagged) >= {"suspicious", "readings", "chosen", "p", "model", "disputed"}
    rows = json.loads((storage / outcome["leaves"][0]["lines"]).read_text(encoding="utf-8"))
    assert any(row["suspicious"] for row in rows)


@needs_slovak
def test_the_line_check_can_be_switched_off_per_message(settings, storage, monkeypatch):
    slovak_leaf(storage / ITEM / "leaves" / "0003.png", lines=["Ťažký ľadový čaj ,,;; stál na stole."])
    monkeypatch.setattr(standalone.linecheck, "check_page", lambda *_a, **_k: pytest.fail("no line check"))

    run(settings, line_check=False)

    outcome = assert_outcome(storage, OUTCOME, status="done")
    assert outcome["leaves"][0]["suspicious"] == [] and outcome["lexicon"] == []


@pytest.mark.ocr
@needs_tesseract
def test_files_ocr_reports_suspicious_lines_in_its_warnings_when_enabled(settings, storage):
    from conftest import kwargs, write_scanned
    from evilflowers_ocr_worker import processing
    from evilflowers_ocr_worker.contract import OcrRequest

    settings = settings.model_copy(update={"ocr_line_check": True})
    request = OcrRequest.model_validate(kwargs())
    write_scanned(storage / request.source)

    outcome = processing.run(request, settings, request.run_id)

    assert outcome.status == "done"
    assert any(warning.startswith("line check: ") for warning in outcome.warnings)


@pytest.mark.ocr
@needs_tesseract
def test_files_ocr_has_no_line_check_by_default(settings, storage, monkeypatch):
    from conftest import kwargs, write_scanned
    from evilflowers_ocr_worker import processing
    from evilflowers_ocr_worker.contract import OcrRequest

    monkeypatch.setattr(processing, "_line_check", lambda *_a: pytest.fail("off by default"))
    request = OcrRequest.model_validate(kwargs())
    write_scanned(storage / request.source)

    assert processing.run(request, settings, request.run_id).status == "done"
