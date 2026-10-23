# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# base: Python + LibreOffice (Writer, Calc and Impress without GUI) + fonts
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS base

LABEL org.opencontainers.image.title="FoldPDF" \
      org.opencontainers.image.description="Self-hosted REST API converting DOCX, XLSX and PPTX to PDF with LibreOffice" \
      org.opencontainers.image.source="https://github.com/MikeFonseta/FoldPDF" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui \
        libreoffice-calc-nogui \
        libreoffice-impress-nogui \
        fonts-dejavu-core \
        fonts-liberation2 \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
        fontconfig \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

# Unprivileged user with fixed UID/GID (handy for volume permissions).
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app

WORKDIR /srv/app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app

ENV HOST=0.0.0.0 \
    PORT=8080 \
    DATA_DIR=/data \
    DATABASE_URL=sqlite:////data/jobs.db \
    LIBREOFFICE_BIN=/usr/bin/soffice \
    HOME=/home/app

RUN mkdir -p /data && chown app:app /data && chmod 700 /data

# ---------------------------------------------------------------------------
# test: same image + development dependencies and tests (unit + integration)
# ---------------------------------------------------------------------------
FROM base AS test
COPY requirements-dev.txt pyproject.toml ./
RUN pip install -r requirements-dev.txt
COPY tests ./tests
USER app
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider", "-v"]

# ---------------------------------------------------------------------------
# runtime: production image (default target)
# ---------------------------------------------------------------------------
FROM base AS runtime
USER app
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8080'), timeout=4)"]
CMD ["python", "-m", "app"]
