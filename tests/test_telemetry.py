"""Issue #4 telemetry with an SDK provider on (in-memory exporters from conftest)."""

import pytest
import responses

from conftest import METRICS, SPANS, kwargs, needs_tesseract, write_scanned
from evilflowers_ocr_worker import jobs
from evilflowers_ocr_worker.observability import setup

URL = kwargs()["report_url"]


def metric_points() -> dict[str, list]:
    data = METRICS.get_metrics_data()
    found: dict[str, list] = {}
    for resource in data.resource_metrics if data else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                found.setdefault(metric.name, []).extend(metric.data.data_points)
    return found


@pytest.mark.ocr
@needs_tesseract
@responses.activate
def test_ocr_run_span_and_metrics(settings, storage):
    SPANS.clear()
    args = kwargs()
    write_scanned(storage / args["source"], pages=2)
    responses.post(URL, status=200)

    jobs.run("22222222-2222-2222-2222-222222222222", args, settings)

    (span,) = [span for span in SPANS.get_finished_spans() if span.name == "ocr.run"]
    attributes = dict(span.attributes)
    assert attributes["evilflowers.job.id"] == args["job_id"]
    assert attributes["evilflowers.run.id"] == args["run_id"]
    assert attributes["evilflowers.acquisition.id"] == args["acquisition_id"]
    assert attributes["ocr.engine"] == "tesseract"
    assert attributes["ocr.outcome"] == "done"
    assert attributes["ocr.pages"] == 2
    assert attributes["ocr.pages_ocred"] == 2

    points = metric_points()
    # Exported to Mimir as ocr_pages_total, ocr_duration_seconds, ocr_reports_total.
    assert {"ocr.pages", "ocr.duration", "ocr.reports"} <= set(points)
    pages = {tuple(sorted(p.attributes.items())): p.value for p in points["ocr.pages"]}
    assert pages[(("engine", "tesseract"), ("outcome", "ocred"))] >= 2
    assert any(dict(p.attributes) == {"status": "200"} for p in points["ocr.reports"])


def test_failed_run_counts_pages_as_failed():
    from evilflowers_ocr_worker.observability import metrics as worker_metrics

    worker_metrics.record_run("rapidocr", "failed", 1.0, pages=7, pages_ocred=None)
    pages = {tuple(sorted(p.attributes.items())): p.value for p in metric_points()["ocr.pages"]}
    assert pages[(("engine", "rapidocr"), ("outcome", "failed"))] >= 7
    assert (("engine", "rapidocr"), ("outcome", "skipped")) not in pages


def test_endpoint_is_derived_from_host_ip(monkeypatch):
    for name in setup.ENDPOINT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOST_IP", "10.0.0.7")
    assert setup.endpoint() == "http://10.0.0.7:4318"


def test_no_endpoint_no_telemetry(monkeypatch):
    for name in setup.ENDPOINT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HOST_IP", raising=False)
    assert setup.endpoint() is None


def test_resource_defaults_to_service_ocr(monkeypatch):
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    resource = setup.build_resource()
    assert resource.attributes["service.name"] == "ocr"
    assert resource.attributes["service.namespace"] == "elvira"
