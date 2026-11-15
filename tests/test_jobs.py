"""The run end to end (issue #5): what lands in the outcome file, what is kept, what is raised."""

import ast
from pathlib import Path

import pytest

from conftest import TOKEN, kwargs, needs_tesseract, read_outcome, write_born_digital, write_encrypted, write_scanned
from evilflowers_ocr_worker import jobs, outcome

TASK_ID = "22222222-2222-2222-2222-222222222222"
FIELDS = {
    "run_id",
    "token",
    "status",
    "output",
    "pages",
    "pages_ocred",
    "skipped_reason",
    "error",
    "warnings",
    "engine",
    "engine_version",
    "language",
    "seconds",
}
SOURCE = Path(__file__).parents[1] / "src" / "evilflowers_ocr_worker"


def outcome_files(storage: Path) -> list[Path]:
    return [path for path in storage.rglob("*.json")]


def leftovers(storage: Path) -> list[Path]:
    return [path for path in storage.rglob("*") if path.name.endswith(outcome.PARTIAL_SUFFIX)]


@pytest.mark.ocr
@needs_tesseract
def test_done_writes_destination_then_the_outcome(settings, storage, monkeypatch):
    args = kwargs()
    write_scanned(storage / args["source"])
    order = []
    real = outcome.os.replace
    root = storage.resolve()

    def spy(source, target):
        real(source, target)
        if Path(target).is_relative_to(root):
            order.append(Path(target).relative_to(root).as_posix())

    monkeypatch.setattr(outcome.os, "replace", spy)
    result = jobs.run(TASK_ID, args, settings)

    assert order == [args["destination"], args["outcome"]], "the outcome file is the commit marker: last"
    written = read_outcome(storage, args)
    assert set(written) == FIELDS
    assert written["run_id"] == args["run_id"]
    assert written["token"] == TOKEN
    assert written["status"] == "done"
    assert written["output"] == args["destination"]
    assert (written["pages"], written["pages_ocred"]) == (1, 1)
    assert (written["engine"], written["language"]) == ("tesseract", "eng")
    assert written["engine_version"]
    assert (storage / args["destination"]).is_file()
    assert outcome_files(storage) == [storage / args["outcome"]]
    assert not leftovers(storage)
    assert result == {
        "status": "done",
        "pages": 1,
        "pages_ocred": 1,
        "engine": "tesseract",
        "seconds": result["seconds"],
    }


@needs_tesseract
def test_skipped_writes_the_outcome_and_no_destination(settings, storage):
    args = kwargs()
    write_born_digital(storage / args["source"])

    result = jobs.run(TASK_ID, args, settings)

    written = read_outcome(storage, args)
    assert set(written) == FIELDS
    assert (written["status"], written["token"]) == ("skipped", TOKEN)
    assert written["skipped_reason"] == "all pages already have text"
    assert written["output"] is None
    assert result["status"] == "skipped"
    assert not (storage / args["destination"]).exists()
    assert outcome_files(storage) == [storage / args["outcome"]]


def test_failed_writes_the_outcome_then_raises(settings, storage, tmp_path):
    args = kwargs()
    write_encrypted(storage / args["source"], tmp_path)

    with pytest.raises(jobs.OcrFailed, match="encrypted") as raised:
        jobs.run(TASK_ID, args, settings)

    written = read_outcome(storage, args)
    assert set(written) == FIELDS
    assert (written["status"], written["token"]) == ("failed", TOKEN)
    assert "encrypted" in written["error"]
    assert raised.value.__cause__ is not None, "the original exception is chained for the traceback"
    assert not (storage / args["destination"]).exists()


def test_processing_crash_writes_a_failed_outcome(settings, storage, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(jobs.processing, "run", boom)
    with pytest.raises(jobs.OcrFailed, match="disk on fire"):
        jobs.run(TASK_ID, kwargs(), settings)
    assert read_outcome(storage, kwargs())["status"] == "failed"


def test_soft_time_limit_writes_a_timeout(settings, storage, monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded

    def slow(*_args, **_kwargs):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(jobs.processing, "run", slow)
    with pytest.raises(jobs.OcrFailed, match="time limit"):
        jobs.run(TASK_ID, kwargs(), settings)
    assert "time limit" in read_outcome(storage, kwargs())["error"]


def test_interrupted_run_writes_no_outcome(settings, storage, monkeypatch):
    """SIGTERM mid-OCR surfaces as SystemExit: a `failed` file would consume the token the redelivery needs."""

    def terminated(*_args, **_kwargs):
        raise SystemExit("terminated during OCR")

    monkeypatch.setattr(jobs.processing, "run", terminated)
    with pytest.raises(SystemExit):
        jobs.run(TASK_ID, kwargs(), settings)
    assert outcome_files(storage) == []


@pytest.mark.ocr
@needs_tesseract
def test_crash_between_destination_and_outcome_leaves_no_outcome(settings, storage, monkeypatch):
    """The catalog then sees no outcome and the run ends `lost`, never applied. The worker deletes nothing."""
    args = kwargs()
    write_scanned(storage / args["source"])
    real = outcome.os.replace

    def crash_on_outcome(source, target):
        if str(target).endswith(".json"):
            raise OSError("node lost power")
        real(source, target)

    monkeypatch.setattr(outcome.os, "replace", crash_on_outcome)
    with pytest.raises(OSError, match="node lost power"):
        jobs.run(TASK_ID, args, settings)

    assert outcome_files(storage) == []
    assert not leftovers(storage)
    assert (storage / args["destination"]).is_file(), "the catalog cleans up, not the worker"


@needs_tesseract
def test_redelivery_overwrites_its_own_outcome(settings, storage):
    args = kwargs()
    write_born_digital(storage / args["source"])
    jobs.run(TASK_ID, args, settings)
    jobs.run(TASK_ID, args, settings)
    assert outcome_files(storage) == [storage / args["outcome"]]
    assert read_outcome(storage, args)["token"] == TOKEN


@needs_tesseract
def test_unknown_kwargs_and_an_old_report_url_are_ignored(settings, storage):
    args = kwargs(priority=5, something_new={"a": 1}, report_url="http://catalog.test/report")
    write_born_digital(storage / args["source"])
    assert jobs.run(TASK_ID, args, settings)["status"] == "skipped"


@pytest.mark.parametrize("missing", ["source", "outcome", "token"])
def test_missing_kwargs_are_a_contract_error(settings, storage, missing):
    args = kwargs()
    del args[missing]
    with pytest.raises(jobs.InvalidRequest, match=missing):
        jobs.run(TASK_ID, args, settings)
    assert outcome_files(storage) == []


def test_contract_error_does_not_leak_the_token(settings):
    args = kwargs(language=None)
    with pytest.raises(jobs.InvalidRequest) as raised:
        jobs.run(TASK_ID, args, settings)
    assert TOKEN not in str(raised.value)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/outcome.json",
        "../outside.json",
        kwargs()["source"],
        kwargs()["destination"],
    ],
)
def test_bad_outcome_path_is_refused_before_ocr(settings, storage, monkeypatch, path):
    monkeypatch.setattr(jobs.processing, "run", lambda *_a, **_k: pytest.fail("OCR must not start"))
    with pytest.raises(jobs.InvalidRequest, match="outcome"):
        jobs.run(TASK_ID, kwargs(outcome=path), settings)


def test_the_token_is_not_in_the_result_or_the_logs(settings, storage, caplog):
    args = kwargs()
    write_born_digital(storage / args["source"])
    with caplog.at_level("DEBUG"):
        result = jobs.run(TASK_ID, args, settings)
    assert TOKEN not in repr(result)
    assert TOKEN not in caplog.text


def test_no_http_client_and_no_api_key(monkeypatch):
    """The worker talks to the catalog through storage and Celery events only."""
    monkeypatch.delenv("CATALOG_API_KEY", raising=False)
    clients = {"requests", "httpx", "urllib3", "aiohttp", "http.client", "urllib.request"}
    imported = set()
    for module in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(module.read_text())):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
    assert not {name for name in imported if name.split(".")[0] in clients or name in clients}
    from evilflowers_ocr_worker.settings import Settings

    assert not any("catalog" in name or "report" in name for name in Settings.model_fields)
