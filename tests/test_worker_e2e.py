"""A real worker (prefork, one child, task events on) against a real Redis, and no catalog at all.

Proves what unit tests cannot: ocrmypdf runs inside a daemonic prefork child, the
outcome file lands on the shared volume with the run's token, the soft time limit
stops a long book and turns into a `failed` outcome before the hard kill, and
SIGTERM mid-OCR hands the message back to Redis without leaving a destination or
an outcome, so another worker finishes the same run and writes the outcome with
the same token. Runs only with TEST_REDIS_URL (CI starts a Redis service).
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from conftest import needs_tesseract, write_scanned

REDIS = os.environ.get("TEST_REDIS_URL")
SOFT_SHUTDOWN = 2.0
pytestmark = [
    pytest.mark.skipif(not REDIS, reason="TEST_REDIS_URL is not set"),
    pytest.mark.ocr,
    needs_tesseract,
]


class Run:
    """One dispatched run: its paths on the volume and its token."""

    def __init__(self, storage: Path, source: str):
        self.storage = storage
        self.source = source
        self.id = str(uuid.uuid4())
        self.token = f"token-{uuid.uuid4().hex}"
        self.destination = f"catalogs/c/e/ocr/{self.id}.pdf"
        self.outcome = f"catalogs/c/e/ocr/{self.id}.json"

    @property
    def outcome_path(self) -> Path:
        return self.storage / self.outcome

    def wait(self, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while not self.outcome_path.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        return json.loads(self.outcome_path.read_text()) if self.outcome_path.exists() else None


@pytest.fixture
def queue():
    import redis

    name = f"ocr-test-{uuid.uuid4().hex[:8]}"
    yield name
    redis.Redis.from_url(REDIS).delete(name)


def start_worker(storage: Path, queue: str, scratch: Path, **env) -> subprocess.Popen:
    scratch.mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "BROKER": REDIS,
        "OCR_QUEUE": queue,
        "STORAGE_PATH": str(storage),
        "TMPDIR": str(scratch),
        "LOG_FORMAT": "text",
        "REMAP_SIGTERM": "SIGQUIT",
        "WORKER_SOFT_SHUTDOWN_TIMEOUT": str(SOFT_SHUTDOWN),
        **env,
    }
    environment.pop("CATALOG_API_KEY", None)
    environment.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    environment.pop("HOST_IP", None)
    # A file, not a pipe: nobody reads a pipe while the worker runs, and a full pipe blocks the worker.
    log = (scratch.parent / f"{scratch.name}.log").open("w")
    worker = subprocess.Popen(
        [
            sys.executable, "-m", "celery", "-A", "evilflowers_ocr_worker.worker", "worker", "-E",
            f"--queues={queue}", "--concurrency=1", "--loglevel=INFO", f"--hostname=ocr@test-{uuid.uuid4().hex[:6]}",
        ],
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
    )  # fmt: skip
    worker.log_path = Path(log.name)
    log.close()
    return worker


def stop(worker: subprocess.Popen, timeout: float = 60) -> str:
    if worker.poll() is None:
        worker.send_signal(signal.SIGTERM)
    try:
        worker.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait()
    return worker.log_path.read_text(errors="replace")


def send(queue: str, run: Run):
    from celery import Celery

    producer = Celery(broker=REDIS)
    return producer.send_task(
        "evilflowers_ocr_worker.ocr",
        task_id=run.id,
        queue=queue,
        kwargs={
            "job_id": str(uuid.uuid4()),
            "run_id": run.id,
            "acquisition_id": str(uuid.uuid4()),
            "source": run.source,
            "destination": run.destination,
            "outcome": run.outcome,
            "token": run.token,
            "language": "eng",
            "redo": False,
            "added_later_by_the_catalog": True,
        },
    )


def wait_for(condition, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.1)


def queued_ids(queue: str) -> list[str]:
    import redis

    client = redis.Redis.from_url(REDIS)
    return [json.loads(raw)["headers"]["id"] for raw in client.lrange(queue, 0, -1)]


def test_prefork_worker_ocrs_and_writes_the_outcome(storage, tmp_path, queue):
    write_scanned(storage / "catalogs/c/e/book.pdf", pages=2)
    run = Run(storage, "catalogs/c/e/book.pdf")
    worker = start_worker(storage, queue, tmp_path / "scratch")
    try:
        send(queue, run)
        body = run.wait(timeout=180)
        # The outcome file is written before the task returns; give `succeeded` a moment to be logged.
        wait_for(lambda: "succeeded" in worker.log_path.read_text(errors="replace"), 30, "task-succeeded")
    finally:
        log = stop(worker)

    assert body, f"no outcome file; worker log:\n{log}"
    assert (body["run_id"], body["token"], body["status"]) == (run.id, run.token, "done"), body
    assert (body["pages"], body["pages_ocred"]) == (2, 2)
    assert body["output"] == run.destination
    assert (storage / run.destination).is_file()
    assert run.token not in log, "the token is never logged"


def test_soft_time_limit_stops_a_long_book_before_the_hard_kill(storage, tmp_path, queue):
    """60 pages take far longer than 20 s; the `failed` outcome must come from the soft limit, not never."""
    write_scanned(storage / "catalogs/c/e/long.pdf", pages=60)
    run = Run(storage, "catalogs/c/e/long.pdf")
    hard = 20
    worker = start_worker(storage, queue, tmp_path / "scratch", TASK_SOFT_TIME_LIMIT="3", TASK_TIME_LIMIT=str(hard))
    try:
        sent = time.monotonic()
        send(queue, run)
        body = run.wait(timeout=hard + 30)
        elapsed = time.monotonic() - sent
    finally:
        log = stop(worker)

    assert body, f"no outcome before the hard limit (ocrmypdf did not stop); worker log:\n{log}"
    assert (body["status"], body["token"]) == ("failed", run.token), body
    assert "time limit" in body["error"]
    assert elapsed < hard, f"outcome after {elapsed:.1f} s"
    assert not (storage / run.destination).exists()


def test_sigterm_mid_ocr_requeues_and_the_redelivery_writes_the_outcome(storage, tmp_path, queue):
    write_scanned(storage / "catalogs/c/e/book.pdf", pages=40)
    run = Run(storage, "catalogs/c/e/book.pdf")
    destination = storage / "catalogs/c/e/ocr"
    scratch = tmp_path / "scratch"

    first = start_worker(storage, queue, scratch)
    try:
        send(queue, run)
        # ocrmypdf has started once the run's scratch directory exists.
        wait_for(lambda: any(scratch.glob(f"ocr-{run.id}-*")), 60, "OCR to start")
        stopped = time.monotonic()
        first.send_signal(signal.SIGTERM)
        first.wait(timeout=SOFT_SHUTDOWN + 20)
        took = time.monotonic() - stopped
    finally:
        log = stop(first)

    assert took < SOFT_SHUTDOWN + 15, log
    assert not run.outcome_path.exists(), "an interrupted run must not write an outcome"
    assert not list(destination.glob("*.pdf")), "no destination after SIGTERM"
    assert queued_ids(queue) == [run.id], f"the message must be back in the queue; log:\n{log}"

    second = start_worker(storage, queue, tmp_path / "scratch2")
    try:
        body = run.wait(timeout=300)
    finally:
        log = stop(second)

    assert body, log
    assert (body["status"], body["token"]) == ("done", run.token), "the redelivery carries the same token"
    assert body["pages_ocred"] == 40
    assert (storage / body["output"]).is_file()
    assert not [path for path in destination.iterdir() if path.name.endswith(".partial")]
