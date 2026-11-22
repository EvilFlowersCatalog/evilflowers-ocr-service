#!/usr/bin/env bash
# Report one deploy to LGTM: an OTLP/JSON log record (Loki) and a `deploy` span (Tempo), POSTed with curl to the
# node's Alloy. Contract: knowledge note CI-Deploy-Unification, "What it sends" — the emitter is CI itself
# (service.name=github-actions, service.namespace=ci), never the deployed service.
#
#   otlp-deploy.sh <succeeded|failed> <start-unix-nanos>
#
# Inputs from the environment: ELVIRA_OTLP_HTTP_ENDPOINT (set on runner pods by roles/github_runner; when unset the
# event is skipped with a warning), NAMESPACE, DEPLOYMENT, ENVIRONMENT, IMAGE, DIGEST, WORKER_REVISION and the
# standard GITHUB_* variables. Telemetry never fails a deploy: every error ends in a warning and exit 0.
set -uo pipefail

outcome="${1:?succeeded|failed}"
start_ns="${2:?start time in unix nanoseconds}"
end_ns="$(date +%s%N)"

if [ -z "${ELVIRA_OTLP_HTTP_ENDPOINT:-}" ]; then
  echo "::warning::ELVIRA_OTLP_HTTP_ENDPOINT is not set on this runner (roles/github_runner not applied): deploy event not sent"
  exit 0
fi

hex() { od -An -tx1 -N"$1" /dev/urandom | tr -d ' \n'; }
trace_id="$(hex 16)"
span_id="$(hex 8)"

# Every value is a GitHub context or a digest/commit; escape anyway so a branch name cannot break the JSON.
esc() { local s="${1//\\/\\\\}"; printf '%s' "${s//\"/\\\"}"; }
attr() { printf '{"key":"%s","value":{"stringValue":"%s"}}' "$1" "$(esc "$2")"; }

run_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
resource="$(attr service.name github-actions),$(attr service.namespace ci),$(attr deployment.environment.name "$ENVIRONMENT"),$(attr k8s.namespace.name "$NAMESPACE")"
common="$(attr vcs.repository.url.full "${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}")"
common+=",$(attr vcs.ref.head.name "$GITHUB_REF_NAME"),$(attr vcs.ref.head.revision "$GITHUB_SHA")"
common+=",$(attr cicd.pipeline.name "$GITHUB_WORKFLOW"),$(attr cicd.pipeline.run.id "$GITHUB_RUN_ID")"
common+=",$(attr cicd.pipeline.run.url.full "$run_url")"
common+=",$(attr oci.manifest.digest "$DIGEST"),$(attr container.image.name "$IMAGE")"
common+=",$(attr evilflowers.worker.revision "$WORKER_REVISION"),$(attr evilflowers.deploy.trigger "$GITHUB_EVENT_NAME")"

if [ "$outcome" = succeeded ]; then severity=9 level=INFO status=1; else severity=17 level=ERROR status=2; fi
scope='{"name":"evilflowers-ocr-service/deploy"}'

logs=$(printf '{"resourceLogs":[{"resource":{"attributes":[%s]},"scopeLogs":[{"scope":%s,"logRecords":[{"timeUnixNano":"%s","severityNumber":%s,"severityText":"%s","body":{"stringValue":"deployment %s %s"},"traceId":"%s","spanId":"%s","attributes":[%s,%s,%s,%s]}]}]}]}' \
  "$resource" "$scope" "$end_ns" "$severity" "$level" "$(esc "$DEPLOYMENT")" "$outcome" "$trace_id" "$span_id" \
  "$(attr event.name deployment)" "$(attr k8s.deployment.name "$DEPLOYMENT")" "$(attr deployment.status "$outcome")" "$common")

spans=$(printf '{"resourceSpans":[{"resource":{"attributes":[%s]},"scopeSpans":[{"scope":%s,"spans":[{"traceId":"%s","spanId":"%s","name":"deploy","kind":1,"startTimeUnixNano":"%s","endTimeUnixNano":"%s","status":{"code":%s},"attributes":[%s,%s]}]}]}]}' \
  "$resource" "$scope" "$trace_id" "$span_id" "$start_ns" "$end_ns" "$status" \
  "$(attr evilflowers.deployments "$DEPLOYMENT")" "$common")

post() {
  curl -fsS --max-time 10 -H 'Content-Type: application/json' -d "$2" "${ELVIRA_OTLP_HTTP_ENDPOINT%/}/v1/$1" >/dev/null \
    || echo "::warning::OTLP $1 not accepted by ${ELVIRA_OTLP_HTTP_ENDPOINT}"
}
post logs "$logs"
post traces "$spans"

echo "trace_id=${trace_id}" >> "${GITHUB_OUTPUT:-/dev/null}"
echo "Deploy event sent: deployment ${DEPLOYMENT} ${outcome}, trace ${trace_id}"
exit 0
