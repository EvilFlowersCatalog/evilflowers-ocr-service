"""ocrmypdf plugin: counts the pages ocrmypdf OCRs, and stops a run cooperatively at its deadline.

Pages counted
    ``filter_ocr_image`` is called once for every page that ``is_ocr_required`` lets
    through, and for no other. Pages skipped because they already have text (skip
    mode), have no image, or are excluded never reach it, so the count is
    ocrmypdf's own per-page decision, not a re-implementation of it.

Deadline
    Celery's soft time limit cannot stop ocrmypdf. It raises in the main thread,
    ocrmypdf's thread pool absorbs the exception and goes on OCRing every queued
    page until the hard limit kills the process, and then no outcome is written
    (measured: tests/test_worker_e2e.py). So the run carries its own deadline, which
    is checked before every page is rasterised and again before it is OCR'd. Past
    the deadline each remaining page fails at once, and ocrmypdf returns within
    about one page's time.

Termination
    The same holds for SIGTERM. Billiard's child handler raises ``SystemExit`` in the
    main thread, and ocrmypdf's ``with ThreadPoolExecutor`` waits for every queued
    page before letting it through, so a pod being stopped would keep OCRing until
    SIGKILL. ``interruptible()`` chains the termination signals while ocrmypdf runs:
    the run is marked aborted, remaining pages fail at once, and the original
    handler's exception (``SystemExit``) propagates unchanged. No outcome file is
    written, and Redis gets the message back (acks_late).

Pages run in ocrmypdf's worker THREADS (``use_threads=True``, forced by
``processing``); the state is process memory guarded by a lock, keyed by the run's
output file so two runs never mix.
"""

import contextlib
import signal
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from ocrmypdf import hookimpl

#: Signals that stop the run: the kit's cold cancel (billiard terminates the child: TERM; QUIT), Ctrl+C,
#: billiard's soft limit.
STOP_SIGNALS = tuple(
    sig for sig in (getattr(signal, name, None) for name in ("SIGTERM", "SIGINT", "SIGQUIT", "SIGUSR1")) if sig
)


class DeadlineExceeded(Exception):
    """The run passed its deadline; remaining pages are not OCR'd."""


class Aborted(Exception):
    """The process is being stopped; remaining pages are not OCR'd."""


@dataclass
class _Run:
    deadline: float | None
    pages: set[int] = field(default_factory=set)
    aborted: bool = False


_lock = threading.Lock()
_runs: dict[str, _Run] = {}


def start(key: str, deadline: float | None = None) -> None:
    """Register a run. ``deadline`` is a ``time.monotonic()`` value."""
    with _lock:
        _runs[key] = _Run(deadline)


def finish(key: str) -> set[int]:
    """Forget a run; returns the 0-based page numbers that were OCR'd."""
    with _lock:
        run = _runs.pop(key, None)
    return run.pages if run else set()


def is_aborted(key: str) -> bool:
    with _lock:
        run = _runs.get(key)
    return bool(run and run.aborted)


def abort(key: str) -> None:
    with _lock:
        if (run := _runs.get(key)) is not None:
            run.aborted = True


@contextlib.contextmanager
def interruptible(key: str) -> Iterator[None]:
    """While the block runs, a stop signal aborts run ``key`` and then runs the previous handler."""
    if threading.current_thread() is not threading.main_thread():
        yield  # signal handlers can only be set from the main thread
        return

    previous: dict[int, object] = {}

    def handler(signum, frame):
        abort(key)
        original = previous.get(signum)
        if callable(original):
            original(signum, frame)
        elif original == signal.SIG_DFL and signum != getattr(signal, "SIGUSR1", None):
            raise SystemExit(128 + signum)

    for sig in STOP_SIGNALS:
        with contextlib.suppress(OSError, ValueError, RuntimeError):
            previous[sig] = signal.signal(sig, handler)
    try:
        yield
    finally:
        for sig, original in previous.items():
            with contextlib.suppress(OSError, ValueError, RuntimeError, TypeError):
                signal.signal(sig, original)


def _check(key: str) -> _Run | None:
    with _lock:
        run = _runs.get(key)
    if run is None:
        return None
    if run.aborted:
        raise Aborted("the worker is stopping")
    if run.deadline is not None and time.monotonic() > run.deadline:
        raise DeadlineExceeded("the OCR run passed its deadline")
    return run


@hookimpl
def rasterize_pdf_page(options, **_kwargs):
    if options is not None:
        _check(str(options.output_file))
    # None: not handled here; ocrmypdf's own rasterizer runs.
    return None


@hookimpl
def filter_ocr_image(page, image):
    key = str(page.options.output_file)
    run = _check(key)
    if run is not None:
        with _lock:
            run.pages.add(page.pageno)
    # The same object back: ocrmypdf then knows the image is unchanged and skips re-encoding it.
    return image
