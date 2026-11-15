"""Issue #4 telemetry, through the kit since #6, with an SDK provider on (in-memory exporters from conftest)."""

import os

import pytest
from evilflowers_worker.observability.setup import build_resource

from conftest import METRICS, SPANS, kwargs, needs_tesseract, write_scanned
from evilflowers_ocr_worker import jobs, metrics
from evilflowers_ocr_worker.contract import OcrRequest


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
def test_ocr_run_span_and_metrics(settings, storage):
    SPANS.clear()
    args = kwargs()
    write_scanned(storage / args["source"], pages=2)

    jobs.run(OcrRequest.model_validate(args), "22222222-2222-2222-2222-222222222222", settings)

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
    # Exported to Mimir as ocr_pages_total, ocr_duration_seconds, ocr_job_outcome_total.
    assert {"ocr.pages", "ocr.duration", "ocr.job.outcome"} <= set(points)
    assert "ocr.reports" not in points
    pages = {tuple(sorted(p.attributes.items())): p.value for p in points["ocr.pages"]}
    assert pages[(("engine", "tesseract"), ("outcome", "ocred"))] >= 2
    assert any(dict(p.attributes) == {"job": "ocr", "outcome": "done"} for p in points["ocr.job.outcome"])
    assert all(args["token"] not in str(value) for value in attributes.values())


def test_failed_run_counts_pages_as_failed():
    metrics.record_run("rapidocr", "failed", 1.0, pages=7, pages_ocred=None)
    pages = {tuple(sorted(p.attributes.items())): p.value for p in metric_points()["ocr.pages"]}
    assert pages[(("engine", "rapidocr"), ("outcome", "failed"))] >= 7
    assert (("engine", "rapidocr"), ("outcome", "skipped")) not in pages


def test_resource_keeps_service_ocr(monkeypatch):
    """The dashboards query service.name="ocr"; the kit's default would be evilflowers-ocr."""
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    assert os.environ["OTEL_SERVICE_NAME"] == "ocr"
    resource = build_resource()
    assert resource.attributes["service.name"] == "ocr"
    assert resource.attributes["service.namespace"] == "elvira"
