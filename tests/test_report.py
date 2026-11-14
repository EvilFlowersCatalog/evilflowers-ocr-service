"""Issue #3: a mocked catalog answering each status, and the headers the report carries."""

import pytest
import requests
import responses
from opentelemetry import trace

from evilflowers_ocr_worker import report

URL = "http://catalog.test/api/v1/jobs/runs/22222222-2222-2222-2222-222222222222/report"
BODY = {"status": "done", "output": "x.pdf", "pages": 1, "pages_ocred": 1}


class Sleeps(list):
    def __call__(self, seconds):
        self.append(seconds)


@responses.activate
def test_200_is_accepted_with_bearer_and_traceparent(settings):
    responses.post(URL, json={"status": "done"}, status=200)
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("ocr.run") as span:
        delivery = report.send(URL, BODY, settings)
        trace_id = format(span.get_span_context().trace_id, "032x")

    assert delivery is report.Delivery.ACCEPTED
    sent = responses.calls[0].request
    assert sent.headers["Authorization"] == "Bearer secret-key"
    assert sent.headers["Content-Type"] == "application/json"
    assert sent.headers["traceparent"].split("-")[1] == trace_id
    assert sent.headers["User-Agent"].startswith("evilflowers-ocr-service/")


@pytest.mark.parametrize("status", [400, 401, 403, 422])
@responses.activate
def test_client_errors_fail_without_retry(settings, status):
    responses.post(URL, json={"detail": "nope"}, status=status)
    sleeps = Sleeps()
    with pytest.raises(report.ReportRejected) as raised:
        report.send(URL, BODY, settings, sleep=sleeps)
    assert raised.value.status == status
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_404_finishes(settings):
    responses.post(URL, status=404)
    assert report.send(URL, BODY, settings) is report.Delivery.NOT_FOUND
    assert len(responses.calls) == 1


@responses.activate
def test_409_is_final(settings):
    responses.post(URL, json={"detail": "cancelled"}, status=409)
    sleeps = Sleeps()
    assert report.send(URL, BODY, settings, sleep=sleeps) is report.Delivery.CONFLICT
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_5xx_retries_with_backoff_then_gives_up(settings):
    responses.post(URL, status=502)
    sleeps = Sleeps()
    backoff = settings.model_copy(update={"report_backoff": (30.0, 90.0, 180.0, 300.0)})
    with pytest.raises(report.CatalogUnavailable):
        report.send(URL, BODY, backoff, sleep=sleeps)
    assert len(responses.calls) == 5
    assert sleeps == [30.0, 90.0, 180.0, 300.0]
    assert sum(sleeps) == 600


@responses.activate
def test_5xx_then_success(settings):
    responses.post(URL, status=503)
    responses.post(URL, status=200)
    assert report.send(URL, BODY, settings, sleep=Sleeps()) is report.Delivery.ACCEPTED
    assert len(responses.calls) == 2


@responses.activate
def test_network_error_is_retried(settings):
    responses.post(URL, body=requests.ConnectionError("refused"))
    responses.post(URL, status=200)
    assert report.send(URL, BODY, settings, sleep=Sleeps()) is report.Delivery.ACCEPTED


@responses.activate
def test_no_key_no_authorization_header(settings):
    responses.post(URL, status=200)
    report.send(URL, BODY, settings.model_copy(update={"catalog_api_key": None}))
    assert "Authorization" not in responses.calls[0].request.headers
