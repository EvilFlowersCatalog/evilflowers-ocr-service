"""The run end to end with a mocked catalog: what is reported, kept, deleted and raised."""

import json

import pytest
import requests
import responses

from conftest import kwargs, needs_tesseract, write_born_digital, write_encrypted, write_scanned
from evilflowers_ocr_worker import jobs, report

TASK_ID = "22222222-2222-2222-2222-222222222222"
URL = kwargs()["report_url"]


def body(call) -> dict:
    return json.loads(call.request.body)


@pytest.mark.ocr
@needs_tesseract
@responses.activate
def test_done_is_reported_and_kept(settings, storage):
    args = kwargs()
    write_scanned(storage / args["source"])
    responses.post(URL, json={"status": "done"}, status=200)

    result = jobs.run(TASK_ID, args, settings)

    sent = body(responses.calls[0])
    assert sent == {
        "status": "done",
        "output": args["destination"],
        "pages": 1,
        "pages_ocred": 1,
        "skipped_reason": None,
        "error": None,
        "warnings": sent["warnings"],
    }
    assert result["status"] == "done"
    assert result["report"] == "accepted"
    assert result["engine"] == "tesseract"
    assert (storage / args["destination"]).is_file()


@pytest.mark.ocr
@needs_tesseract
@responses.activate
def test_409_deletes_destination_and_finishes(settings, storage):
    args = kwargs()
    write_scanned(storage / args["source"])
    responses.post(URL, status=409)

    result = jobs.run(TASK_ID, args, settings)

    assert result["report"] == "conflict"
    assert not (storage / args["destination"]).exists()
    assert (storage / args["source"]).exists()


@pytest.mark.ocr
@needs_tesseract
@responses.activate
def test_400_fails_the_task_and_keeps_destination(settings, storage):
    args = kwargs()
    write_scanned(storage / args["source"])
    responses.post(URL, json={"detail": "output mismatch"}, status=400)

    with pytest.raises(report.ReportRejected):
        jobs.run(TASK_ID, args, settings)
    assert (storage / args["destination"]).is_file()


@pytest.mark.ocr
@needs_tesseract
@responses.activate
def test_catalog_down_after_done_keeps_destination(settings, storage):
    """The catalog may have applied an earlier attempt whose answer was lost: never delete then."""
    args = kwargs()
    write_scanned(storage / args["source"])
    responses.post(URL, body=requests.ReadTimeout("read timed out"))
    for _ in range(settings.report_attempts - 1):
        responses.post(URL, status=502)

    with pytest.raises(report.CatalogUnavailable):
        jobs.run(TASK_ID, args, settings)
    assert len(responses.calls) == settings.report_attempts
    assert (storage / args["destination"]).is_file()


@responses.activate
def test_404_finishes(settings, storage):
    args = kwargs()
    write_born_digital(storage / args["source"])
    responses.post(URL, status=404)
    assert jobs.run(TASK_ID, args, settings)["report"] == "not_found"


@needs_tesseract
@responses.activate
def test_skipped_is_reported(settings, storage):
    args = kwargs()
    write_born_digital(storage / args["source"])
    responses.post(URL, status=200)

    result = jobs.run(TASK_ID, args, settings)

    assert result["status"] == "skipped"
    assert body(responses.calls[0])["skipped_reason"] == "all pages already have text"
    assert not (storage / args["destination"]).exists()


@responses.activate
def test_failed_is_reported_then_raised(settings, storage, tmp_path):
    args = kwargs()
    write_encrypted(storage / args["source"], tmp_path)
    responses.post(URL, status=200)

    with pytest.raises(jobs.OcrFailed, match="encrypted") as raised:
        jobs.run(TASK_ID, args, settings)

    sent = body(responses.calls[0])
    assert sent["status"] == "failed"
    assert "encrypted" in sent["error"]
    assert raised.value.__cause__ is not None, "the original exception is chained for the traceback"


@responses.activate
def test_catalog_down_fails_the_task(settings, storage):
    args = kwargs()
    write_born_digital(storage / args["source"])
    responses.post(URL, status=500)
    with pytest.raises(report.CatalogUnavailable):
        jobs.run(TASK_ID, args, settings)
    assert len(responses.calls) == settings.report_attempts


@responses.activate
def test_unknown_kwargs_are_ignored(settings, storage):
    args = kwargs(priority=5, something_new={"a": 1})
    write_born_digital(storage / args["source"])
    responses.post(URL, status=200)
    assert jobs.run(TASK_ID, args, settings)["status"] == "skipped"


def test_missing_kwargs_are_a_contract_error(settings):
    args = kwargs()
    del args["source"]
    with pytest.raises(jobs.InvalidRequest, match="source"):
        jobs.run(TASK_ID, args, settings)


@responses.activate
def test_processing_crash_is_reported_as_failed(settings, storage, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(jobs.processing, "run", boom)
    responses.post(URL, status=200)
    with pytest.raises(jobs.OcrFailed, match="disk on fire"):
        jobs.run(TASK_ID, kwargs(), settings)
    assert body(responses.calls[0])["status"] == "failed"


@responses.activate
def test_soft_time_limit_is_reported_as_timeout(settings, monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded

    def slow(*_args, **_kwargs):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(jobs.processing, "run", slow)
    responses.post(URL, status=200)
    with pytest.raises(jobs.OcrFailed, match="time limit"):
        jobs.run(TASK_ID, kwargs(), settings)
    assert "time limit" in body(responses.calls[0])["error"]
