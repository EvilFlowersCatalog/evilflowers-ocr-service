# syntax=docker/dockerfile:1.7

# -- build: the virtual environment from uv.lock, and the RapidOCR models -------------------------------------------
FROM python:3.12-slim AS build

# git: uv fetches evilflowers-worker from its pinned commit ([tool.uv.sources]).
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first (cached while only the code changes).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --all-extras --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-extras --no-editable

# The root filesystem is read-only at runtime and the pod has no business downloading models, so every
# model is fetched here: detection, the PP-OCRv6 multilingual recognition model (eng → ~50 Latin languages
# incl. slk/ces/deu/hun/pol/fra), the East Slavic model (rus/ukr) and the page orientation model.
ARG RAPIDOCR_LANGUAGES="eng rus"
RUN /app/.venv/bin/python -m ocrmypdf.builtin_plugins.rapidocr_ocr download \
        --model-dir /app/models/rapidocr ${RAPIDOCR_LANGUAGES}

# -- runtime ---------------------------------------------------------------------------------------------------------
FROM python:3.12-slim

# Tesseract with the language packs of issue #2 (+ osd for page rotation), and ocrmypdf's helpers.
# Hunspell with the Slovak, Czech and English dictionaries: the lexicon of the suspicious-line check (issue #8).
ARG TESSERACT_LANGUAGES="slk ces eng deu hun pol rus ukr fra osd"
ARG HUNSPELL_DICTIONARIES="sk cs en-us"
RUN set -eux; \
    packs=""; for lang in ${TESSERACT_LANGUAGES}; do packs="$packs tesseract-ocr-$lang"; done; \
    dicts=""; for dict in ${HUNSPELL_DICTIONARIES}; do dicts="$dicts hunspell-$dict"; done; \
    apt-get update; \
    apt-get install -y --no-install-recommends tesseract-ocr $packs ghostscript unpaper pngquant hunspell $dicts; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/models /app/models

ARG IMAGE_TAG=dev
# No REMAP_SIGTERM: SIGTERM (scale-in, rollout) is a warm shutdown and evilflowers-worker's `shutdown` cancels and
# requeues the book after WORKER_SOFT_SHUTDOWN_TIMEOUT (issue #6). HOME=/tmp: anything that insists on a cache dir
# writes to the emptyDir, never to the read-only root.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    OCRMYPDF_RAPIDOCR_MODEL_DIR=/app/models/rapidocr \
    STORAGE_PATH=/mnt/data \
    IMAGE_TAG=${IMAGE_TAG}

USER 65534:65534

# Prefork with one child: Flower can inspect the worker while a book is OCR'd, and telemetry starts in the
# child (worker_process_init). Pages run in parallel inside it (OCR_JOBS threads). The k8s Deployment adds
# --hostname=ocr@<env>.%h.
CMD ["celery", "-A", "evilflowers_ocr_worker.worker", "worker", "--loglevel=INFO", "-E", "--concurrency=1", \
     "--queues=evilflowers_ocr_worker"]
