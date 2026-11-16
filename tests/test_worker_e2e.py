"""A real worker (prefork, one child, task events on) against a real Redis, and no catalog at all.

Proves what unit tests cannot:

- ocrmypdf runs inside a daemonic prefork child, and the outcome file lands on the
  shared volume with the run's token (issue #5);
- the soft time limit stops a long book and turns into a `failed` outcome before
  the hard kill;
- scale-in under evilflowers-worker's shutdown (issue #6): SIGTERM is warm, the book
  is cancelled after WORKER_SOFT_SHUTDOWN_TIMEOUT and requeued at once, with no
  destination and no outcome, its delivery given back. Another worker finishes the
  same run and writes the outcome with the same token;
- a PDF that kills its worker on every delivery ends with a `failed` outcome past
  MAX_DELIVERIES instead of being redelivered forever;
- an idle worker exits at once on SIGTERM.

Runs only with TEST_REDIS_URL (CI starts a Redis service). The worker's output goes
to a file, not a pipe (the kit's ``testing.WorkerProcess``): nobody reads a pipe while
a book is OCR'd, and a full pipe blocks the worker.
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
from evilflowers_worker.deliveries import DeliveryCounter
from evilflowers_worker.testing import assert_outcome

from conftest import needs_slovak, needs_tesseract, slovak_leaf, write_scanned

REDIS = os.environ.get("TEST_REDIS_URL")
GRACE = 2.0
CANCEL = 3.0
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


def redis_client():
    import redis

    return redis.Redis.from_url(REDIS)


@pytest.fixture
def queue():
    name = f"ocr-test-{uuid.uuid4().hex[:8]}"
    yield name
    redis_client().delete(name)


def start_worker(storage: Path, queue: str, scratch: Path, **env) -> subprocess.Popen:
    scratch.mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "BROKER": REDIS,
        "OCR_QUEUE": queue,
        "STORAGE_PATH": str(storage),
        "TMPDIR": str(scratch),
        "LOG_FORMAT": "text",
        "WORKER_SOFT_SHUTDOWN_TIMEOUT": str(GRACE),
        "WORKER_CANCEL_TIMEOUT": str(CANCEL),
        **env,
    }
    for name in ("CATALOG_API_KEY", "REMAP_SIGTERM", "OTEL_EXPORTER_OTLP_ENDPOINT", "HOST_IP"):
        environment.pop(name, None)
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


def queued(queue: str) -> list[dict]:
    return [json.loads(raw) for raw in redis_client().lrange(queue, 0, -1)]


def queued_kwargs(message: dict) -> dict:
    import base64

    _args, kwargs, _embed = json.loads(base64.b64decode(message["body"]))
    return kwargs


def pool_children(worker: subprocess.Popen) -> list[int]:
    """The prefork children of the worker (not billiard's semaphore tracker, which macOS runs beside them)."""
    found = subprocess.run(["ps", "-ax", "-o", "pid=,ppid=,command="], capture_output=True, text=True, check=True)
    children = []
    for line in found.stdout.splitlines():
        pid, ppid, command = line.split(None, 2)
        if int(ppid) == worker.pid and "semaphore_tracker" not in command:
            children.append(int(pid))
    return children


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
    assert_outcome(storage, run.outcome, run_id=run.id, token=run.token, status="done", after=run.destination)
    assert (body["pages"], body["pages_ocred"]) == (2, 2)
    assert body["output"] == run.destination
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


def test_scale_in_mid_book_requeues_at_once_and_the_redelivery_writes_the_outcome(storage, tmp_path, queue):
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
        wait_for(lambda: len(queued(queue)) == 1, GRACE + CANCEL + 15, "the book back on the queue")
        requeued_after = time.monotonic() - stopped
        first.wait(timeout=GRACE + CANCEL + 20)
    finally:
        log = stop(first)

    assert requeued_after < GRACE + CANCEL + 10, log
    assert not run.outcome_path.exists(), "an interrupted run must not write an outcome"
    assert not list(destination.glob("*.pdf")), "no destination after SIGTERM"
    (message,) = queued(queue)
    assert message["headers"]["id"] == run.id
    assert queued_kwargs(message)["token"] == run.token
    # A deliberate requeue gives its delivery back: stopping a pod never counts towards the cap.
    assert not redis_client().exists(DeliveryCounter(None, prefix="ocr").key(run.id)), log

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


def test_a_pdf_that_kills_its_worker_ends_failed_past_the_cap(storage, tmp_path, queue):
    """The first delivery is killed (an OOM, as far as the parent can tell); past MAX_DELIVERIES=1 it gives up."""
    write_scanned(storage / "catalogs/c/e/book.pdf", pages=40)
    run = Run(storage, "catalogs/c/e/book.pdf")
    scratch = tmp_path / "scratch"
    worker = start_worker(storage, queue, scratch, MAX_DELIVERIES="1")
    try:
        send(queue, run)
        wait_for(lambda: any(scratch.glob(f"ocr-{run.id}-*")), 60, "OCR to start")
        (child,) = pool_children(worker)
        os.kill(child, signal.SIGKILL)
        body = run.wait(timeout=90)
    finally:
        log = stop(worker)

    assert body, f"the cap never wrote an outcome; worker log:\n{log}"
    assert (body["status"], body["token"]) == ("failed", run.token), body
    assert body["error"].startswith("gave up after 1 deliveries"), body
    assert not (storage / run.destination).exists()
    assert queued(queue) == [], "acknowledged: never delivered again"


def test_an_idle_worker_exits_at_once(storage, tmp_path, queue):
    worker = start_worker(storage, queue, tmp_path / "scratch")
    try:
        wait_for(lambda: "ready" in worker.log_path.read_text(errors="replace"), 60, "the worker to start")
        sent = time.monotonic()
        worker.send_signal(signal.SIGTERM)
        code = worker.wait(timeout=20)
        took = time.monotonic() - sent
    finally:
        log = stop(worker)

    assert code == 0, log
    assert took < GRACE + 5, f"idle worker took {took:.1f} s"


@needs_slovak
def test_a_standalone_deployment_writes_hocr_and_the_outcome(storage, tmp_path, queue):
    """The digitalizer's Deployment: the same image on a standalone queue, the task sent by name with kwargs."""
    from celery import Celery

    slovak_leaf(storage / "digitized/item/leaves/0003.png")
    job_dir = "digitized/item/_studio/jobs/1-a1"
    run = Run(storage, "")
    run.outcome = f"{job_dir}/outcome.json"
    worker = start_worker(storage, queue, tmp_path / "scratch")
    try:
        Celery(broker=REDIS).send_task(
            "evilflowers_ocr_worker.standalone.hocr",
            task_id=run.id,
            queue=queue,
            kwargs={
                "run_id": run.id,
                "job": "analyze-hocr-0003-a1",
                "token": run.token,
                "output_dir": job_dir,
                "outcome": run.outcome,
                "leaves": [{"leaf": 3, "image": "digitized/item/leaves/0003.png", "dpi": 300, "language": "slk"}],
            },
        )
        body = run.wait(timeout=180)
        wait_for(lambda: "succeeded" in worker.log_path.read_text(errors="replace"), 30, "task-succeeded")
    finally:
        log = stop(worker)

    assert body, f"no outcome file; worker log:\n{log}"
    assert_outcome(
        storage, run.outcome, run_id=run.id, token=run.token, status="done", after=f"{job_dir}/0003_hocr.html"
    )
    assert "Ťažký" in body["leaves"][0]["text"]
    assert run.token not in log, "the token is never logged"
