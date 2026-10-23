# syntax=docker/dockerfile:1.7
# Сгенерировано deploy-kit (uv-проект). Сборка: deps-слой кэшируется отдельно от кода.
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
# OCR of scans (tesseract 5.3, rus + eng) and .doc/.rtf/.odt (LibreOffice 7.4): research.md T073.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng libreoffice-writer-nogui \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app:app /data
WORKDIR /app
COPY --from=build --chown=app:app /app /app
# One OCR thread per tesseract process: pages run in worker processes already (T073).
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 OMP_THREAD_LIMIT=1 DATA_DIR=/data
VOLUME ["/data"]
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status < 400 else 1)"
CMD ["gr", "serve", "--host", "0.0.0.0"]
