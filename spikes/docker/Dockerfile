# Spike T073: measure image size, OCR speed/quality, DOC->DOCX conversion, volume perms.
# Not the production image (that one comes from /deploy-kit in T056).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OMP_THREAD_LIMIT=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus libreoffice-writer-nogui \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pypdfium2==5.14.0 rapidfuzz==3.14.6 pillow

RUN useradd --uid 10001 --create-home app && mkdir -p /data && chown app:app /data
VOLUME ["/data"]
WORKDIR /spike
COPY measure.py /spike/measure.py
USER app
HEALTHCHECK --interval=10s --timeout=3s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1
CMD ["python", "/spike/measure.py"]
