"""A real worker (prefork, one child, task events on) against a real Redis and a fake catalog over HTTP.

Proves what unit tests cannot: ocrmypdf runs inside a daemonic prefork child, the
report reaches the catalog over the network, the soft time limit stops a long
book and turns into a `failed` report before the hard kill, and SIGTERM mid-OCR
hands the message back to Redis without leaving a destination, so another worker
finishes the same run. Runs only with TEST_REDIS_URL (CI starts a Redis service).
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class Catalog:
    """Records the reports it receives and answers 200."""

    def __init__(self):
        self.reports: list[tuple[str, dict, dict]] = []
        catalog = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                catalog.reports.append((self.path, dict(self.headers), json.loads(self.rfile.read(length))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def wait(self, count: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while len(self.reports) < count and time.monotonic() < deadline:
            time.sleep(0.2)


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
        "CATALOG_API_KEY": "e2e-key",
        "LOG_FORMAT": "text",
        "REPORT_BACKOFF": "[0]",
        "REMAP_SIGTERM": "SIGQUIT",
        "WORKER_SOFT_SHUTDOWN_TIMEOUT": str(SOFT_SHUTDOWN),
        **env,
    }
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


def send(queue: str, catalog: Catalog, source: str, run_id: str):
    from celery import Celery

    producer = Celery(broker=REDIS)
    return producer.send_task(
        "evilflowers_ocr_worker.ocr",
        task_id=run_id,
        queue=queue,
        kwargs={
            "job_id": str(uuid.uuid4()),
            "run_id": run_id,
            "acquisition_id": str(uuid.uuid4()),
            "source": source,
            "destination": f"catalogs/c/e/ocr/{run_id}.pdf",
            "language": "eng",
            "redo": False,
            "report_url": f"{catalog.base}/api/v1/jobs/runs/{run_id}/report",
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


def test_prefork_worker_ocrs_and_reports(storage, tmp_path, queue):
    catalog = Catalog()
    write_scanned(storage / "catalogs/c/e/book.pdf", pages=2)
    worker = start_worker(storage, queue, tmp_path / "scratch")
    try:
        run_id = str(uuid.uuid4())
        send(queue, catalog, "catalogs/c/e/book.pdf", run_id)
        catalog.wait(1, timeout=180)
    finally:
        log = stop(worker)

    assert catalog.reports, f"no report arrived; worker log:\n{log}"
    path, headers, body = catalog.reports[0]
    assert path == f"/api/v1/jobs/runs/{run_id}/report"
    assert headers["Authorization"] == "Bearer e2e-key"
    assert body["status"] == "done", body
    assert (body["pages"], body["pages_ocred"]) == (2, 2)
    assert (storage / body["output"]).is_file()
    assert "succeeded" in log, log


def test_soft_time_limit_stops_a_long_book_before_the_hard_kill(storage, tmp_path, queue):
    """60 pages take far longer than 20 s; the `failed` report must come from the soft limit, not never."""
    catalog = Catalog()
    write_scanned(storage / "catalogs/c/e/long.pdf", pages=60)
    hard = 20
    worker = start_worker(storage, queue, tmp_path / "scratch", TASK_SOFT_TIME_LIMIT="3", TASK_TIME_LIMIT=str(hard))
    try:
        run_id = str(uuid.uuid4())
        sent = time.monotonic()
        send(queue, catalog, "catalogs/c/e/long.pdf", run_id)
        catalog.wait(1, timeout=hard + 30)
        elapsed = time.monotonic() - sent
    finally:
        log = stop(worker)

    assert catalog.reports, f"no report before the hard limit (ocrmypdf did not stop); worker log:\n{log}"
    body = catalog.reports[0][2]
    assert body["status"] == "failed", body
    assert "time limit" in body["error"]
    assert elapsed < hard, f"report after {elapsed:.1f} s"
    assert not (storage / f"catalogs/c/e/ocr/{run_id}.pdf").exists()


def test_sigterm_mid_ocr_requeues_and_another_worker_finishes(storage, tmp_path, queue):
    catalog = Catalog()
    write_scanned(storage / "catalogs/c/e/book.pdf", pages=40)
    destination = storage / "catalogs/c/e/ocr"
    scratch = tmp_path / "scratch"
    run_id = str(uuid.uuid4())

    first = start_worker(storage, queue, scratch)
    try:
        send(queue, catalog, "catalogs/c/e/book.pdf", run_id)
        # ocrmypdf has started once the run's scratch directory exists.
        wait_for(lambda: any(scratch.glob(f"ocr-{run_id}-*")), 60, "OCR to start")
        stopped = time.monotonic()
        first.send_signal(signal.SIGTERM)
        first.wait(timeout=SOFT_SHUTDOWN + 20)
        took = time.monotonic() - stopped
    finally:
        log = stop(first)

    assert took < SOFT_SHUTDOWN + 15, log
    assert catalog.reports == [], "an interrupted run must not report"
    assert not list(destination.glob("*.pdf")), "no destination after SIGTERM"
    assert queued_ids(queue) == [run_id], f"the message must be back in the queue; log:\n{log}"

    second = start_worker(storage, queue, tmp_path / "scratch2")
    try:
        catalog.wait(1, timeout=300)
    finally:
        log = stop(second)

    assert catalog.reports, log
    body = catalog.reports[0][2]
    assert body["status"] == "done", body
    assert body["pages_ocred"] == 40
    assert (storage / body["output"]).is_file()
    assert not [path for path in destination.iterdir() if path.name.endswith(".partial")]
