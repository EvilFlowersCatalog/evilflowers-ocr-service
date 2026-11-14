import json
import logging

from opentelemetry import trace

from evilflowers_ocr_worker.observability.context import bind
from evilflowers_ocr_worker.observability.logs import JsonFormatter


def test_json_line_carries_job_fields_and_trace():
    record = logging.LogRecord("evilflowers_ocr_worker.jobs", logging.INFO, __file__, 1, "OCR %s", ("done",), None)
    with (
        trace.get_tracer("test").start_as_current_span("ocr.run") as span,
        bind(job_id="j", run_id="r", acquisition_id="a", engine="tesseract"),
    ):
        line = json.loads(JsonFormatter().format(record))
        trace_id = format(span.get_span_context().trace_id, "032x")

    assert line["level"] == "info"
    assert line["message"] == "OCR done"
    assert line["trace_id"] == trace_id
    assert (line["job_id"], line["run_id"], line["acquisition_id"], line["engine"]) == ("j", "r", "a", "tesseract")


def test_bound_fields_never_overwrite_reserved_keys():
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "hello", (), None)
    with bind(level="debug", message="spoofed", job_id="j"):
        line = json.loads(JsonFormatter().format(record))
    assert line["level"] == "warning"
    assert line["message"] == "hello"
    assert line["job_id"] == "j"
