# Pinned runtime line; application libraries are pinned in backend/requirements.txt.
FROM python:3.11.11-slim-bookworm

ARG FFMPEG_VERSION=7:5.1.9-0+deb12u1
ARG TESSERACT_VERSION=5.3.0-2
ARG TESSDATA_VERSION=1:4.1.0-2
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MINGJIAN_ENV=production \
    MINGJIAN_RETENTION_SECONDS=900 \
    MINGJIAN_JOB_TIMEOUT_SECONDS=120 \
    ENABLE_ASR=false \
    ENABLE_OCR=false \
    ENABLE_FACE=false \
    ENABLE_SYNC=false \
    ENABLE_RETRIEVAL=false \
    ENABLE_LLM=false

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg=${FFMPEG_VERSION} tesseract-ocr=${TESSERACT_VERSION} tesseract-ocr-eng=${TESSDATA_VERSION} tesseract-ocr-chi-sim=${TESSDATA_VERSION} \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 mingjian

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu torch==2.5.1+cpu \
    && pip install --requirement /app/backend/requirements.txt \
    && pip check \
    && python -c "import cv2, numpy, torch, transformers; print(torch.__version__, torch.version.cuda, numpy.__version__, cv2.__version__, transformers.__version__); assert torch.__version__ == '2.5.1+cpu'; assert torch.version.cuda is None; assert numpy.__version__ == '1.26.4'; assert cv2.__version__ == '4.10.0'; assert transformers.__version__ == '4.57.6'"
COPY backend /app/backend
COPY frontend /app/frontend
RUN mkdir -p /app/backend/data /tmp \
    && chown -R mingjian:mingjian /app/backend/data /tmp

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"
CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
