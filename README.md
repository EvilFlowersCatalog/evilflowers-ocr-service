# evilflowers-ocr-service

Celery worker of the EvilFlowers catalog: adds an invisible text layer to scanned PDFs so they become
searchable and extractable. It runs the catalog's job kind **`files.ocr`** (IP-031) and replaces
[`evilflowers-ocr-worker`](https://github.com/EvilFlowersCatalog/evilflowers-ocr-worker), which overwrote
the original in place and failed on every born-digital PDF (EvilFlowersCatalog#146).

- **Never touches the original.** It writes only `destination`, atomically.
- **Skips born-digital PDFs.** Pages that already have text are left alone, which is not an error.
- **Lets the caller choose the OCR engine.** Tesseract is the default; RapidOCR (PaddleOCR models on
  ONNX Runtime) is the alternative.
- **Reports the outcome** to the catalog, which then replaces the file or adds a derived acquisition, and
  starts LCP re-encryption and extraction as follow-up jobs.

```
catalog ──(Redis db 7, queue evilflowers_ocr_worker)──▶ ocr worker ──▶ ocrmypdf + engine
   ▲                                                       │  reads  STORAGE_PATH/source
   │                                                       │  writes STORAGE_PATH/destination (tmp → fsync → rename)
   └────────── POST /api/v1/jobs/runs/<run_id>/report ◀────┘
```

## Task contract

| | |
|---|---|
| Task | `evilflowers_ocr_worker.ocr` (the old name, so existing routes keep working) |
| Queue | `evilflowers_ocr_worker` (catalog `EVILFLOWERS_OCR_QUEUE`) |
| Task id | the catalog's **run id**; a retry is a new run with a new id |
| Events | the worker runs with `-E`; the catalog builds the job status from task events |

The task takes keyword arguments only, and ignores any it does not know:

```python
ocr(
    job_id: str,          # the job in /api/v1/jobs
    run_id: str,          # = task id
    acquisition_id: str,
    source: str,          # storage-relative, read only
    destination: str,     # storage-relative: catalogs/<catalog>/<entry>/ocr/<run_id>.pdf
    language: str,        # ISO 639-2 alpha3 ("slk", "ces", "eng"; B codes like "slo"/"cze" accepted)
    redo: bool,           # true: replace an existing OCR layer (ocrmypdf redo mode)
    report_url: str,      # POST the outcome here
    engine: str = None,   # optional: "tesseract" | "rapidocr"; None → OCR_DEFAULT_ENGINE
)
```

### Outcomes

| Status | When | Destination |
|---|---|---|
| `done` | at least one page was OCR'd | written |
| `skipped` | every page already has text, or the PDF is tagged or signed (ocrmypdf leaves those alone) | none |
| `failed` | password-protected, corrupt, missing source, unknown language, engine not available, timeout | none; the task raises after reporting |

A language the chosen engine cannot read is `failed`. The worker never silently falls back to English.

### Report

`POST {report_url}` with `Authorization: Bearer $CATALOG_API_KEY` and `traceparent`:

```json
{"status": "done", "output": "catalogs/…/ocr/<run_id>.pdf", "pages": 214, "pages_ocred": 198,
 "skipped_reason": null, "error": null, "warnings": []}
```

| Catalog answers | Worker |
|---|---|
| 200 | done |
| 400, 401, 403 | fails the task, no retry |
| 404 | logs and finishes |
| 409 (cancelled, or a newer run replaced this one) | **deletes the destination**, finishes |
| 5xx or a network error | retries 5 times over about 10 min (`REPORT_BACKOFF`), then fails the task |

Only 409 deletes the destination. In every other case the catalog may already have applied an earlier
attempt whose answer was lost, so deleting the file could break a published book; an orphaned file is the
cheaper failure.

### Time limits and shutdown

- **`TASK_SOFT_TIME_LIMIT` (3.5 h) is the run's own deadline.** The worker checks it before each page is
  rasterised and OCR'd, so the run stops within about one page and reports `failed` ("time limit exceeded").
- **Celery's soft time limit is not used.** ocrmypdf's thread pool absorbs Celery's asynchronous
  `SoftTimeLimitExceeded` and keeps OCRing every queued page until the hard kill, so no report would ever be
  sent (`tests/test_worker_e2e.py` showed this).
- **`TASK_TIME_LIMIT` (3 h 55 min) is Celery's hard kill.** The gap between the soft and hard limits covers
  one page's worst case (`TESSERACT_TIMEOUT`) plus the report retries.
- **On SIGTERM** (rollout, scale-in; `REMAP_SIGTERM=SIGQUIT` makes it a cold shutdown), the remaining pages
  are abandoned at once:
  - no report is sent and no destination is written;
  - the scratch directory is removed;
  - Redis gets the message back (`acks_late`), so another worker redoes the same run. The test suite
    checks all of this end to end.

The task's return value becomes the job's `result` in the ledger. It holds the report plus
`engine`, `engine_version`, `language`, `seconds` and `report` (`accepted`, `conflict`, `not_found`).

## OCR engines

Every engine is an [ocrmypdf](https://ocrmypdf.readthedocs.io) OCR engine plugin, so the engines share the
rest of the pipeline: rasterising, page rotation, deskew, skipping pages that have text, grafting the text
layer, and writing the output. Adding an engine means writing one `Engine` subclass in
`src/evilflowers_ocr_worker/engines.py`: a plugin name, a language map and its options.

| Engine | What | Languages | Status |
|---|---|---|---|
| **`tesseract`** (default) | Tesseract 5.5 LSTM | `slk ces eng deu hun pol rus ukr fra` (the image's packs) | in the image |
| **`rapidocr`** | PaddleOCR PP-OCRv6 (Latin/CJK) and PP-OCRv5 (Cyrillic…) on ONNX Runtime, CPU only. Built into ocrmypdf ≥ 17.14 | about 50 Latin languages (all of the above), Cyrillic, Greek, Arabic, CJK… | in the image, models baked in |

```bash
python -m evilflowers_ocr_worker engines                   # engines, versions, languages (JSON)
python -m evilflowers_ocr_worker selftest -l slk rus       # OCR a generated page with every enabled engine
```

> **The catalog cannot choose an engine yet.** Its `OcrSpec` has only `redo`, and `OcrDefinition.kwargs()`
> does not send `engine`, so every catalog job uses `OCR_DEFAULT_ENGINE`. The worker side is ready: a task
> sent with `engine="rapidocr"` uses it. Exposing the choice is a catalog change: an `engine` field on
> `OcrSpec`, passed through in `kwargs()`.

### Why Tesseract stays the default (measured 2026-11-14)

Synthetic A4 pages of Slovak and Czech text with diacritics (ť ľ ď ô ŕ ě ř ů), slightly rotated and blurred.
Each engine ran once with skip mode, rotation and deskew, on an M-series Mac (CPU). Treat the timings as
relative. "Lines exact" counts how many of the five distinct lines came back exactly.

| Scan | Engine | Lines exact | Time / page | Typical errors |
|---|---|---|---|---|
| 300 dpi, clean, Slovak | tesseract | **5/5** | **5.1 s** | none |
| | rapidocr | 3/5 | 23 s (first call loads models) | `ť ľ ď` read as `t' l' d'` |
| 300 dpi, clean, Czech | tesseract | **5/5** | **2.5 s** | none |
| | rapidocr | 4/5 | 8.4 s | `ť ď` read as `t' d'` |
| 150 dpi, noisy, Slovak | tesseract | **4/5** | **1.8 s** | stray punctuation from noise |
| | rapidocr | 3/5 | 7.5 s | carons as apostrophes, but no noise garbage |
| 150 dpi, noisy, Czech | tesseract | **5/5** | **1.6 s** | stray punctuation from noise |
| | rapidocr | 3/5 | 6.7 s | `í ů` lost |

On Slovak and Czech, Tesseract is both more accurate and 3–4× faster on CPU. RapidOCR is worth choosing per
request for noisy scans where Tesseract invents punctuation, and for scripts outside the Tesseract packs in
the image. Multi-column and table layouts were **not** measured; ocrmypdf's RapidOCR engine does its own
layout analysis, which may read them better. Benchmark on real scans from the catalog before changing the
default.

### Engines considered, not tested

These are notes from a desk review. Nothing in this table was installed or measured, so verify licences,
sizes and hardware needs before acting on it.

| Candidate | Why not now | When it becomes worth it |
|---|---|---|
| **VLM OCR** (olmOCR-2 7B, PaddleOCR-VL 0.9B, dots.ocr, DeepSeek-OCR, Granite-Docling 258M) | Best quality on hard layouts and handwriting, but they need a GPU. On control's CPUs (Ollama: ~9 tokens/s) one page takes minutes. Most return text or Markdown without word boxes, so a searchable text layer needs layout-aware alignment. | When the GPU from ADR-0011 is in place: add a `vlm` engine that calls a vLLM/Ollama endpoint, uses PP-OCR detection for the boxes and lets the VLM supply the text. |
| **docTR** (Mindee) | Good word boxes, but it needs PyTorch (an image of about 2 GB), is slower than RapidOCR on CPU, and its Slovak vocabulary is weaker | Only if RapidOCR's layout handling proves insufficient |
| **Surya** | Strong multilingual accuracy, but its model licence restricts commercial use, and it needs PyTorch and a GPU for reasonable speed | Licence review first |
| **EasyOCR** | PyTorch, the slowest on CPU, weaker than PP-OCRv6 | No |
| **Kraken / Calamari** | Built for historical prints (Fraktur, early modern) and need trained models per collection | A digitisation project for old prints (see `evilflowers-books-digitalizer`) |

## Configuration

Everything comes from the environment (or `.env`, see `.env.sample`).

| Variable | Default | |
|---|---|---|
| `BROKER` (or `REDIS_URL`) | `redis://redis:6379/7` | Celery broker |
| `OCR_QUEUE` | `evilflowers_ocr_worker` | |
| `STORAGE_PATH` | `/mnt/data` | the shared data volume |
| `CATALOG_API_KEY` | — | catalog service user with `tasks.report_job` |
| `OCR_DEFAULT_ENGINE` | `tesseract` | engine used when the task names none |
| `OCR_ENABLED_ENGINES` | `["tesseract","rapidocr"]` | engines a caller may choose |
| `OCR_JOBS` | `1` | pages OCR'd in parallel; **keep it equal to the CPU limit** |
| `TASK_SOFT_TIME_LIMIT` / `TASK_TIME_LIMIT` | `12600` / `14100` s | run deadline / Celery hard kill; both below the catalog's `EVILFLOWERS_OCR_TIME_LIMIT` (4 h) |
| `BROKER_VISIBILITY_TIMEOUT` | `43200` s | above the hard limit, and **equal to the other consumers of Redis db 7**; the shortest value wins for everyone (knowledge#30) |
| `REPORT_ATTEMPTS` / `REPORT_BACKOFF` | `5` / `[30,90,180,300]` | retries on 5xx |
| `TESSERACT_TIMEOUT` | `600` s | per page |
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO`, `json` | |
| `OTEL_*`, `HOST_IP` | — | telemetry stays off without an endpoint (Deploy-a-Service §3) |

## Telemetry

Telemetry follows the knowledge note **Deploy-a-Service**. It starts in the prefork child
(`worker_process_init`) and exports over OTLP/HTTP to `http://$(HOST_IP):4318`. The pod needs the label
`elvira.stuba.sk/instrumentation: otel-sdk`.

- **Spans:**
  - the Celery consumer span;
  - `ocr.run`, carrying `evilflowers.job.id`, `evilflowers.run.id`, `evilflowers.acquisition.id`,
    `ocr.engine`, `ocr.pages`, `ocr.pages_ocred` and `ocr.outcome`;
  - the report's HTTP client span, which sends `traceparent` so the catalog's span joins the trace.
- **Metrics:**
  - `ocr_pages_total{outcome,engine}`
  - `ocr_duration_seconds{engine,outcome}`
  - `ocr_reports_total{status}`
  - `ocr_task_duration_seconds`
  - process CPU and memory
- **Logs:** one JSON object per line, with `level`, `trace_id`, `job_id`, `run_id`, `acquisition_id` and
  `engine`.

Queries: Tempo `{resource.service.name="ocr"}`; Loki `{service_name="ocr"} | json`.

## Development

```bash
uv sync --all-extras                 # Python 3.12; system Tesseract + Ghostscript for the OCR tests
uv run pytest -q                     # unit tests and real OCR on generated PDFs
docker run -d --rm -p 6390:6379 redis:7
env TEST_REDIS_URL=redis://localhost:6390/15 uv run pytest -q tests/test_worker_e2e.py   # real prefork worker
uv run ruff check && uv run ruff format --check
docker build -t ocr .
docker run --rm --read-only --network none --user 65534 --tmpfs /tmp ocr python -m evilflowers_ocr_worker selftest -l slk rus
```

## Deployment

The worker runs as the Deployment `ocr` in `evilflowers-dev`, next to lcpencrypt and the extraction workers.
The Ansible role `evilflowers_k8s` in EvilFlowersCatalog/knowledge manages it:

- the data PVC is mounted at `STORAGE_PATH`;
- `/tmp` is an emptyDir, and the root filesystem is read-only;
- the pod runs as uid 65534;
- the command is
  `celery -A evilflowers_ocr_worker.worker worker -E --queues=evilflowers_ocr_worker --concurrency=1 --hostname=ocr@dev.%h`.

CI (`.github/workflows/image.yml`) works like this:

1. **Check:** lint, then tests against a real Redis.
2. **Build:** builds the image and smoke-tests it read-only and offline.
3. **Push:** pushes `ghcr.io/evilflowerscatalog/evilflowers-ocr-service:{master,sha-<short>}`.
4. **Deploy:** runs `rollout restart` on `elvira-k3s`, but only once the repository variable
   `DEPLOY_DEV=true` is set.
