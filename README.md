# evilflowers-ocr-service

Celery worker of the EvilFlowers catalog: adds an invisible text layer to scanned PDFs so they become
searchable and extractable. It runs the catalog's job kind **`files.ocr`** (IP-031) and replaces
[`evilflowers-ocr-worker`](https://github.com/EvilFlowersCatalog/evilflowers-ocr-worker), which overwrote
the original in place and failed on every born-digital PDF (EvilFlowersCatalog#146).

- **Never touches the original.** It writes only `destination`, atomically.
- **Skips born-digital PDFs.** Pages that already have text are left alone, which is not an error.
- **Lets the caller choose the OCR engine.** Tesseract is the default; RapidOCR (PaddleOCR models on
  ONNX Runtime) is the alternative.
- **Writes the outcome to a file** next to the output. The catalog reads it when the Celery event arrives
  (or on its reconcile tick) and then replaces the file or adds a derived acquisition, and starts LCP
  re-encryption and extraction as follow-up jobs. The worker needs no catalog URL and no API key.

```
catalog ──(Redis db 7, queue evilflowers_ocr_worker)──▶ ocr worker ──▶ ocrmypdf + engine
   ▲  ▲                                                    │  reads  STORAGE_PATH/source
   │  │                                                    │  writes STORAGE_PATH/destination  (tmp → fsync → rename)
   │  │                                                    │  writes STORAGE_PATH/outcome      (last: the commit marker)
   │  └──────── task-succeeded / task-failed (-E) ◀────────┤
   └─────────── reads STORAGE_PATH/outcome ◀───────────────┘
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
    outcome: str,         # storage-relative: catalogs/<catalog>/<entry>/ocr/<run_id>.json
    token: str,           # the run's capability token (catalog-minted); echoed back, never logged
    language: str,        # ISO 639-2 alpha3 ("slk", "ces", "eng"; B codes like "slo"/"cze" accepted)
    redo: bool,           # true: replace an existing OCR layer (ocrmypdf redo mode)
    engine: str = None,   # optional: "tesseract" | "rapidocr"; None → OCR_DEFAULT_ENGINE
)
```

A message without `outcome` or `token` fails the task at once with a contract error; the worker never falls
back to an HTTP report. `report_url` from an older catalog is ignored like any other unknown kwarg. An
`outcome` outside `STORAGE_PATH`, or equal to `source` or `destination`, is refused before OCR starts.

### Outcomes

| Status | When | Destination | Outcome file |
|---|---|---|---|
| `done` | at least one page was OCR'd | written first | written last |
| `skipped` | every page already has text, or the PDF is tagged or signed (ocrmypdf leaves those alone) | none | written |
| `failed` | password-protected, corrupt, missing source, unknown language, engine not available, timeout | none | written, then the task raises |
| interrupted | SIGTERM / cold shutdown mid-book | none | **none**; the message goes back to Redis |

A language the chosen engine cannot read is `failed`. The worker never silently falls back to English.

### Outcome file

The contract is the catalog's (EvilFlowersCatalog#169). It is written atomically (`.partial`, fsync,
rename, fsync the directory), **after** `destination`: it is the commit marker. A crash between the two
leaves no outcome file, so the catalog ends the run `lost` instead of applying it.

```json
{"run_id": "…", "token": "…", "status": "done", "output": "catalogs/…/ocr/<run_id>.pdf",
 "pages": 214, "pages_ocred": 198, "skipped_reason": null, "error": null, "warnings": [],
 "engine": "tesseract", "engine_version": "5.5.0", "language": "slk", "seconds": 412.3}
```

The worker never deletes anything it wrote. When a run was revoked or replaced by a newer one, or its
token is rejected, the catalog deletes `destination` and the outcome file itself.

An interrupted run must never write a `failed` outcome: that would consume the token, and the catalog
would refuse the outcome of the redelivery, which carries the same token.

### Time limits and shutdown

- **`TASK_SOFT_TIME_LIMIT` (3 h 40 min) is the run's own deadline.** The worker checks it before each page
  is rasterised and OCR'd, so the run stops within about one page and writes a `failed` outcome ("time limit
  exceeded").
- **Celery's soft time limit is not used.** ocrmypdf's thread pool absorbs Celery's asynchronous
  `SoftTimeLimitExceeded` and keeps OCRing every queued page until the hard kill, so no outcome would ever
  be written (`tests/test_worker_e2e.py` showed this).
- **`TASK_TIME_LIMIT` (3 h 55 min) is Celery's hard kill.** The 15-minute gap between the soft and hard
  limits covers one page's worst case (`TESSERACT_TIMEOUT`, 10 min) plus writing the destination and the
  outcome file (5 min allowed).
- **On SIGTERM** (rollout, scale-in), evilflowers-worker's `shutdown` takes over. There is no
  `REMAP_SIGTERM` and no Celery soft shutdown any more; that path redid a finished book on dev.
  - SIGTERM is a warm shutdown. An idle worker exits at once. A book in hand runs on for
    `WORKER_SOFT_SHUTDOWN_TIMEOUT` (10 s). If it finishes, it writes its outcome and is acknowledged.
  - After the grace, the cold cancel terminates the child and the remaining pages are abandoned. No destination
    and no outcome file are written, the scratch directory is removed, and Redis gets the message back at once.
    The redelivery carries the same token. If even the cancel does not stop the child within
    `WORKER_CANCEL_TIMEOUT` (10 s), the worker requeues the message itself and exits.
  - A deliberate requeue gives its delivery back, so stopping a pod never counts towards the cap.
  - **Short grace, pod grace unchanged.** A book takes minutes to hours and almost never finishes inside any
    grace, so a longer one only delays the requeue. 10 s + 10 s (plus the fallback's Redis call) stays under the
    pod's `terminationGracePeriodSeconds` of 30 s (knowledge#54). Raise both together, or the pod is SIGKILLed
    before the requeue and the book waits out the 12 h visibility timeout.
- **A PDF that kills the worker** (OOM) is redelivered (`acks_late`, `reject_on_worker_lost`) at most
  `MAX_DELIVERIES` (5) times. The delivery after that writes a `failed` outcome ("gave up after N deliveries")
  and raises, instead of looping forever.

The test suite (`tests/test_worker_e2e.py`) checks every case end to end against a real Redis.

The task's return value becomes the job's `result` in the ledger. It is kept compact (`status`, `pages`,
`pages_ocred`, `engine`, `seconds`) because Celery's event carries a repr capped at 1024 characters. The
full outcome lives in the file.

## Standalone hOCR for the books digitalizer

A second task, **`evilflowers_ocr_worker.standalone.hocr`**, OCRs the books digitalizer's cleaned leaves to
hOCR with the same engines (issue #7; design: evilflowers-books-digitalizer `docs/design/target-architecture.md`
§2–§3). It has no catalog and no PDF: each leaf image goes straight to the engine (`Engine.recognize`).
`files.ocr` is unchanged.

![Standalone hOCR](docs/diagrams/sequence-standalone-hocr.svg)

| | |
|---|---|
| Queues | `evilflowers_ocr_worker.standalone` (bulk, up to 32 leaves, KEDA 0..N) and `evilflowers_ocr_worker.standalone.interactive` (1 leaf, one replica always on) on the digitalizer's Redis **db 8** |
| Deployment | the same image, `BROKER=redis://…/8`, `OCR_QUEUE=<one of the queues>`, one Deployment per queue |
| Callbacks | the orchestrator sends with `link` / `link_error`; the return value stays compact (`job`, `status`, `leaves`, `suspicious`) |

```python
standalone_hocr(
    run_id: str, job: str,      # echoed back
    token: str,                 # echoed back, never logged
    output_dir: str,            # job-scoped: <item>/_studio/jobs/<job>/
    outcome: str,               # written last
    leaves: [{"leaf": 3, "image": "digitized/…/0003.tif", "dpi": 600,
              "language": "slk+eng", "mask": [[10, 2210, 50, 2250]]}],
    engine: str = None,         # "tesseract" | "rapidocr"; None → OCR_DEFAULT_ENGINE
    line_check: bool = None,    # None → STANDALONE_LINE_CHECK
)
```

- Every path is relative to `STORAGE_PATH`. The leaf image is only read: `mask` boxes (`[x0, y0, x1, y1]`, x1 and
  y1 exclusive) are painted white on a private copy in scratch space.
- `language` is a `+`-joined spec. Every part must be read by the engine; a missing one fails the job, never
  falls back to English.
- Per leaf, written atomically into `output_dir`: `NNNN_hocr.html` (one `ocr_page`, ids relabelled to the IA
  convention `page_%06d`, `line_%06d_N`) and `NNNN_lines.json` (every line: id, bbox, text, confidence, verdict).
- The outcome file, last and atomic: `{run_id, token, status, error, leaves: [{leaf, hocr, text, mean_conf,
  lines, suspicious}], engine, engine_version, lexicon, seconds}`. `mean_conf` is 0..100 over words.
- `failed` is written and then raised (Celery fires `link_error`); a stop (SIGTERM) writes nothing and the
  message is redelivered; past `MAX_DELIVERIES` a `failed` outcome is written. An invalid message whose
  `outcome` and `token` are usable gets a `failed` outcome naming the contract error.
- The run deadline (`TASK_SOFT_TIME_LIMIT`) is checked before every leaf and bounds each Tesseract process.

The message is the kit's `StandaloneJob` (evilflowers-worker#5) plus `leaves`, `engine` and `line_check`. The kit
requires `inputs`; the digitalizer sends only `leaves`, so a message without `inputs` gets the leaves' images.

### Suspicious lines (issue #8)

Tesseract invents punctuation on noisy scans and RapidOCR turns carons into apostrophes (the benchmark below).
The line check (`linecheck.py`) flags such lines and accepts a correction only when two engines agree.

- **Signals, no model.** A line is suspicious when any of these fires: mean confidence under
  `LINE_CHECK_CONFIDENCE` **and** a word outside the hunspell lexicon; broken diacritics (`t'`, `l'`, `d'` in a
  Slovak or Czech line, at any confidence); a run of stray punctuation; a hyphenation artefact (`slo- vo`, `¬`).
- **Second reading.** The other engine (Tesseract ↔ RapidOCR) reads the line's crop. The correction is accepted
  only when both readings are equal after normalisation (NFC, apostrophes, `t'` → `ť`, quotes, dashes, spacing)
  **and** at most `max(2, LINE_CHECK_MAX_EDIT_RATIO × length)` edits apart (`t'` → `ť` is one edit). `chosen` is
  then the reading with broken diacritics repaired, but only where the second reading has the caron: an apostrophe
  both engines read (`it's`, `d'Artagnan`) stays. Otherwise the line is `disputed` with both readings and
  `chosen: null`.
- **Judge (optional).** A line still disputed with two readings is put to `decider` (Laya, text only) through the
  kit's `judges` (evilflowers-worker#6): "which reading is the correct text?" (`a`, `b`, `neither`). Its `p`,
  `model@revision`, `verdict` and `band` are stored on the line. The dispute is settled only in the calibrated
  `keep` band (`JUDGE_THRESHOLDS={"ocr_line_reading": {"keep": …, "hide": …}}`, knowledge#65); without thresholds
  the line stays disputed. Off unless `JUDGES_ENABLED=true` and `DECIDER_URL` are set; one budget per job
  (`JUDGE_BUDGET_CALLS`, `JUDGE_BUDGET_SECONDS`); a judge that does not answer changes nothing.
- **Output.** Per suspicious line, in the outcome: `{line, bbox, suspicious, reasons, readings, chosen,
  disputed, p, model, verdict, band}`. The hOCR itself is never rewritten: `chosen` is a proposal for the
  digitalizer's review screen.
- **`files.ocr`.** With `OCR_LINE_CHECK=true`, every OCR'd page is checked and the findings go to the outcome's
  `warnings` (a `line check: N suspicious lines, M disputed` summary first). Off by default: ocrmypdf keeps no
  per-line confidence, so each page is OCR'd a second time.
- **Lexicon.** Hunspell `sk_SK`, `cs_CZ`, `en_US` (in the image). A language without an installed dictionary gets
  no lexicon signal; the outcome's `lexicon` lists the dictionaries actually used.

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
| `STORAGE_PATH` | `/mnt/data` | the shared data volume (`source`, `destination`, `outcome`) |
| `OCR_DEFAULT_ENGINE` | `tesseract` | engine used when the task names none |
| `OCR_ENABLED_ENGINES` | `["tesseract","rapidocr"]` | engines a caller may choose |
| `OCR_JOBS` | `1` | pages OCR'd in parallel; **keep it equal to the CPU limit** |
| `TASK_SOFT_TIME_LIMIT` / `TASK_TIME_LIMIT` | `13200` / `14100` s | run deadline / Celery hard kill; both below the catalog's `EVILFLOWERS_OCR_TIME_LIMIT` (4 h) |
| `BROKER_VISIBILITY_TIMEOUT` | `43200` s | above the hard limit, and **equal to the other consumers of Redis db 7**; the shortest value wins for everyone (knowledge#30) |
| `TESSERACT_TIMEOUT` | `600` s | per page |
| `RAPIDOCR_MAX_SIDE_LEN` | `2000` px | standalone hOCR: RapidOCR downscales a longer leaf |
| `STANDALONE_LINE_CHECK` / `OCR_LINE_CHECK` | `true` / `false` | the suspicious-line check in the standalone task / in `files.ocr` |
| `LINE_CHECK_CONFIDENCE` | `80` | a line under it with an unknown word is suspicious |
| `LINE_CHECK_MAX_EDIT_RATIO` | `0.15` | largest accepted correction, relative to the line |
| `LINE_CHECK_MAX_LINES` | `40` | second readings per page, at most |
| `HUNSPELL_DICTIONARIES` | `{"slk":"sk_SK","ces":"cs_CZ","eng":"en_US"}` | lexicon per language |
| `JUDGES_ENABLED` (or `DECIDER_ENABLED`), `DECIDER_URL`, `DECIDER_REVISION`, `JUDGE_THRESHOLDS`, `JUDGE_BUDGET_*` | off | the kit's `JudgeSettings`: the line check's `decider` (Deploy-Decision-Models) |
| `WORKER_SOFT_SHUTDOWN_TIMEOUT` / `WORKER_CANCEL_TIMEOUT` | `10` / `10` s | scale-in grace / cold cancel; together under the pod's `terminationGracePeriodSeconds` (30 s) |
| `MAX_DELIVERIES` | `5` | deliveries of one message before the book is given up `failed` (`0`: no cap) |
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO`, `json` | |
| `OTEL_*`, `HOST_IP` | — | telemetry stays off without an endpoint (Deploy-a-Service §3) |

## Telemetry

Telemetry comes from [evilflowers-worker](https://github.com/EvilFlowersCatalog/evilflowers-worker)'s
`observability` and follows the knowledge note **Deploy-a-Service**. It starts in the prefork child
(`worker_process_init`) and exports over OTLP/HTTP to `http://$(HOST_IP):4318`. The pod needs the label
`elvira.stuba.sk/instrumentation: otel-sdk`. `service.name` stays `ocr` (the worker sets `OTEL_SERVICE_NAME`
unless the pod does).

- **Spans:**
  - the Celery consumer span;
  - `ocr.run`, carrying the job-ledger attributes `evilflowers.job.id`, `evilflowers.run.id` and
    `evilflowers.job.delivery`, plus `evilflowers.acquisition.id`, `ocr.engine`, `ocr.language`,
    `ocr.pages`, `ocr.pages_ocred` and `ocr.outcome`. The acquisition keeps the spelling the dashboards
    use; the kit's canonical one is `evilflowers.acquisition_id` (an open question in its architecture doc).
- **Metrics:**
  - the kit's standard set: `ocr_task_duration_seconds`, `ocr_job_outcome_total{job="ocr",outcome}`
    (outcome files written) and `ocr_deliveries_capped_total`;
  - OCR's own: `ocr_pages_total{outcome,engine}` and `ocr_duration_seconds{engine,outcome}`;
  - process CPU and memory.
- **Logs:** one JSON object per line, with `level`, `trace_id`, `job_id`, `run_id`, `acquisition_id` and
  `engine`.

Queries: Tempo `{resource.service.name="ocr"}`; Loki `{service_name="ocr"} | json`.

`service.version` is the image's `IMAGE_TAG`, `<ref>@<commit>+worker.<worker commit, 12 chars>`, so any span or
`target_info` series says which evilflowers-worker the pod ran. The full worker commit is also the image label
`sk.stuba.elvira.evilflowers-worker.revision` and the variable `EVILFLOWERS_WORKER_REVISION`.

## Development

```bash
uv sync --all-extras                 # Python 3.12; system Tesseract + Ghostscript for the OCR tests
                                     # evilflowers-worker: the commit uv.lock last resolved from its master;
uv lock --upgrade-package evilflowers-worker   # catch up with the worker's master (CI does this on every run)
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

The standalone Deployments for the books digitalizer (namespace `digitalizer`, Redis db 8) are not deployed
yet: they belong to the knowledge repository's §4 wiring. They run the same image with
`--queues=evilflowers_ocr_worker.standalone` (or `….standalone.interactive`) and the matching `OCR_QUEUE`.

CI (`.github/workflows/image.yml`) works like this:

1. **Resolve:** `uv lock --upgrade-package evilflowers-worker` picks the worker's current `master`
   (pyproject tracks the branch, not a commit). The resulting uv.lock goes to the next jobs, and nothing is
   committed back.
2. **Check:** lint, then tests against a real Redis, on that lock.
3. **Build:** builds the image and smoke-tests it read-only and offline. It also checks that the installed worker
   is the resolved commit.
4. **Push:** pushes `ghcr.io/evilflowerscatalog/evilflowers-ocr-service:{master,sha-<short>}`.
5. **Deploy** (default branch only): runs `rollout restart` on `elvira-k3s`, checks the digest, and reports the
   deploy to LGTM, succeeded or failed:
   - a Loki event: `{service_name="github-actions"} | event_name="deployment" | k8s_deployment_name="ocr"`;
   - a Tempo span: `{resource.service.name="github-actions" && name="deploy"}`;
   - both carry the commit, the run, the digest and `evilflowers.worker.revision`;
   - the format is the knowledge proposal CI-Deploy-Unification;
   - the event is skipped with a warning until the runner pods have `ELVIRA_OTLP_HTTP_ENDPOINT`.

**Nightly** (02:17 UTC) the same pipeline runs on `master`, so a new evilflowers-worker reaches dev within a day,
without a commit here:
- it builds and deploys only when `:master` does not already carry this commit and the worker's current `master`
  (the image labels say which);
- it pushes `:master` only, and `:sha-<short>` keeps naming the image of the push.
