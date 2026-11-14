"""What a task is working on, carried to its span and to every log line it writes.

``bind(job_id=..., run_id=..., acquisition_id=...)`` puts the values on the current
span under their semantic attribute names and into a context variable that
``logs.JsonFormatter`` adds to each line under the short names. A Loki query for
``job_id`` then finds every line of a job, even outside a sampled trace.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any

from evilflowers_ocr_worker.observability.otel import trace

#: Short (log) name → span attribute name. Other keys keep their name on both.
SPAN_ATTRIBUTES = {
    "job_id": "evilflowers.job.id",
    "run_id": "evilflowers.run.id",
    "acquisition_id": "evilflowers.acquisition.id",
    "engine": "ocr.engine",
    "language": "ocr.language",
}

_fields: ContextVar[Mapping[str, Any]] = ContextVar("evilflowers_ocr_fields", default=MappingProxyType({}))


def clean(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the ``None`` values (OpenTelemetry rejects them)."""
    return {key: value for key, value in attributes.items() if value is not None}


def current() -> Mapping[str, Any]:
    return _fields.get()


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Attach ``fields`` to the current span and to every log line written inside the block."""
    fields = clean(fields)
    if trace is not None:
        span = trace.get_current_span()
        for key, value in fields.items():
            span.set_attribute(SPAN_ATTRIBUTES.get(key, key), value)
    token = _fields.set({**_fields.get(), **fields})
    try:
        yield
    finally:
        _fields.reset(token)
