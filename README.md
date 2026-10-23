# FoldPDF

**Self-hosted REST API that converts DOCX, XLSX and PPTX documents to PDF using headless LibreOffice.**

![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-009688.svg?logo=fastapi&logoColor=white)
![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg?logo=docker&logoColor=white)
![Platforms](https://img.shields.io/badge/platform-Linux%20%7C%20Windows%20%7C%20macOS-lightgrey.svg)

No Microsoft Office, no cloud services, no commercial APIs: every conversion runs locally on the machine that
hosts the service.

```bash
curl -X POST "http://127.0.0.1:8080/jobs?wait=30" -H "X-API-Key: $API_KEY" \
     -F "file=@report.docx" -o report.pdf
```

## Features

- **DOCX → PDF, XLSX → PDF, PPTX → PDF** through LibreOffice Writer, Calc and Impress.
- **Asynchronous job API**: upload, get a `job_id`, poll the status, download the PDF.
- **Single-call mode**: `POST /jobs?wait=30` returns the PDF directly if it is ready within 30 seconds.
- **API key authentication** (`X-API-Key`), checked before the request body is read.
- **Two equivalent ways to run it**: natively (Python + LibreOffice) or with Docker Compose. Same code, same endpoints.
- **Persistent jobs** in SQLite: jobs survive restarts; interrupted conversions are retried or marked as failed.
- **Robust conversion**: isolated working directory and LibreOffice profile per job, timeout, process-tree kill,
  PDF sanity checks.
- **Input validation**: size limit, chunked reads, ZIP/Office Open XML structure checks, zip-bomb and
  path-traversal protection.
- **Automatic cleanup** of expired jobs and orphaned files, never touching active jobs or running downloads.
- **Interactive docs** at `/docs` (Swagger UI).

## Table of contents

- [Quick start](#quick-start)
- [Supported formats](#supported-formats)
- [Requirements](#requirements)
- [Native installation (without Docker)](#native-installation-without-docker)
- [Running as a service](#running-as-a-service)
- [Docker Compose](#docker-compose)
- [Configuration](#configuration)
- [API](#api)
- [Client examples](#client-examples)
- [Integrating it into an application](#integrating-it-into-an-application)
- [HTTP status codes and errors](#http-status-codes-and-errors)
- [Architecture](#architecture)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)
- [Layout fidelity and limitations](#layout-fidelity-and-limitations)
- [Security, persistence and retention](#security-persistence-and-retention)
- [Contributing](#contributing)
- [License](#license)

## Quick start

**With Docker**

```bash
git clone https://github.com/MikeFonseta/FoldPDF.git
cd FoldPDF
echo "API_KEYS=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" > .env
docker compose up --build -d
curl http://127.0.0.1:8080/ready
```

**Without Docker** (Python 3.12+ and LibreOffice installed)

```bash
git clone https://github.com/MikeFonseta/FoldPDF.git
cd FoldPDF
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Then open http://127.0.0.1:8080/docs.

## Supported formats

| Input | Output | LibreOffice component |
|---|---|---|
| `.docx` (Word) | PDF | Writer |
| `.xlsx` (Excel) | PDF | Calc |
| `.pptx` (PowerPoint) | PDF | Impress |

Rejected: legacy binary formats (`.doc`, `.xls`, `.ppt`), macro-enabled files (`.docm`, ...) even when renamed,
encrypted/password-protected documents and any file whose content does not match its extension.

## Requirements

| Mode | Requirements |
|---|---|
| Native | Python ≥ 3.12, LibreOffice 7.x or newer (Writer, Calc, Impress), Git (optional) |
| Docker | Docker Engine 24+ or Docker Desktop with the `docker compose` plugin |

Each concurrent conversion (`MAX_WORKERS`) needs roughly 300–500 MB of RAM.

## Native installation (without Docker)

### 1. Install LibreOffice

**Debian / Ubuntu**

```bash
sudo apt update
sudo apt install -y libreoffice-writer libreoffice-calc libreoffice-impress
# or, on a headless server:
sudo apt install -y --no-install-recommends libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui
# fonts metric-compatible with common Microsoft fonts:
sudo apt install -y fonts-liberation2 fonts-crosextra-carlito fonts-crosextra-caladea fonts-dejavu-core
soffice --version
```

**Fedora / RHEL / Rocky**

```bash
sudo dnf install -y libreoffice-writer libreoffice-calc libreoffice-impress \
  liberation-fonts google-carlito-fonts google-caladea-fonts
```

**Windows**

```powershell
winget install --id TheDocumentFoundation.LibreOffice -e
& "C:\Program Files\LibreOffice\program\soffice.exe" --version
```

The installer does not add LibreOffice to `PATH`; the service finds it in the standard location anyway.

**macOS**

```bash
brew install --cask libreoffice
/Applications/LibreOffice.app/Contents/MacOS/soffice --version
```

### 2. Install Python

- Linux: `sudo apt install -y python3 python3-venv python3-pip` or `sudo dnf install -y python3.12`
- Windows: `winget install --id Python.Python.3.12 -e` (or the installer from python.org, with *Add to PATH*)
- macOS: `brew install python@3.12`

### 3. Create the virtual environment and install dependencies

```bash
git clone https://github.com/MikeFonseta/FoldPDF.git
cd FoldPDF
python -m venv .venv
```

| Shell | Activation |
|---|---|
| Linux / macOS | `source .venv/bin/activate` |
| Windows PowerShell | `.\.venv\Scripts\Activate.ps1` (if blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`) |
| Windows cmd | `.venv\Scripts\activate.bat` |

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt   # only to run the tests
```

### 4. Configure (optional)

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Defaults work out of the box. If LibreOffice is not on `PATH` nor in a standard location, set `LIBREOFFICE_BIN`:

```dotenv
LIBREOFFICE_BIN=C:\Program Files\LibreOffice\program\soffice.exe            # Windows, no quotes needed
LIBREOFFICE_BIN=/Applications/LibreOffice.app/Contents/MacOS/soffice         # macOS
LIBREOFFICE_BIN=/usr/bin/soffice                                             # Linux
```

`LIBREOFFICE_BIN` also accepts the installation directory or a command name on `PATH`. When empty, the service
looks for `soffice`/`libreoffice` on `PATH`, then in the standard installation paths of each OS.

### 5. Start the server

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8080
# or, using HOST and PORT from the configuration:
python -m app
```

| URL | Meaning |
|---|---|
| `/health` | `{"status":"ok"}`: the API process is running |
| `/ready` | `200` when conversions can be processed, `503` with the reason otherwise |
| `/docs` | Swagger UI |

The startup log explicitly says whether the service is ready or LibreOffice is unavailable (in which case new
jobs are rejected with `503`).

## Running as a service

Bind to `127.0.0.1` and publish through a reverse proxy (nginx, Caddy, IIS) with TLS, or set `HOST=0.0.0.0` to
listen on all interfaces. **Run a single process per data directory**: do not use `uvicorn --workers N`.

**Linux (systemd)**: a hardened unit is provided in [`deploy/foldpdf.service`](deploy/foldpdf.service).

```bash
sudo useradd --system --home /var/lib/foldpdf --shell /usr/sbin/nologin foldpdf
sudo mkdir -p /var/lib/foldpdf && sudo chown foldpdf: /var/lib/foldpdf && sudo chmod 700 /var/lib/foldpdf
sudo git clone https://github.com/MikeFonseta/FoldPDF.git /opt/foldpdf
cd /opt/foldpdf && sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo cp deploy/foldpdf.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now foldpdf
```

**Windows**: run `.\.venv\Scripts\python.exe -m app`, or register it as a Windows service with [NSSM](https://nssm.cc/):

```powershell
nssm install FoldPDF "C:\FoldPDF\.venv\Scripts\python.exe" "-m app"
nssm set FoldPDF AppDirectory "C:\FoldPDF"
nssm set FoldPDF AppEnvironmentExtra "HOST=127.0.0.1" "PORT=8080" "DATA_DIR=C:\ProgramData\FoldPDF"
nssm start FoldPDF
```

**macOS (launchd)**: create a LaunchAgent plist running `.venv/bin/python -m app` with the project as
`WorkingDirectory` and `HOST`, `PORT`, `DATA_DIR` as environment variables.

## Docker Compose

```bash
docker compose up --build -d
docker compose ps               # becomes "healthy"
docker compose logs -f foldpdf
docker compose down             # data is kept in the volume
docker compose down -v          # also DELETES the data volume
```

The image contains Python 3.12, the application, LibreOffice Writer/Calc/Impress (`nogui` packages) and the
DejaVu, Liberation, Carlito and Caladea fonts. It runs the same code as the native mode (`python -m app`).

- Persistent data (database, uploads, PDFs) lives in the `foldpdf-data` volume mounted at `/data`.
- Runs as non-root user `app` (UID 10001) with a read-only root filesystem, `cap_drop: ALL`,
  `no-new-privileges`, tmpfs `/tmp` and `init: true` to reap LibreOffice child processes.
- The port is published on `127.0.0.1:8080` of the host by default.

Variables read by Compose (shell environment or a `.env` file next to `compose.yaml`):

```dotenv
API_KEYS=your-long-random-key     # strongly recommended
FOLDPDF_BIND=0.0.0.0               # expose the port on all host interfaces
FOLDPDF_PORT=9000                  # host port
MAX_WORKERS=4
CONVERSION_TIMEOUT_SECONDS=180
```

## Configuration

Settings are defined in `app/config.py`, read from environment variables (highest priority) and from a `.env`
file in the working directory, and validated at startup: an invalid value stops the service with a clear message.

| Variable | Default | Effect |
|---|---|---|
| `HOST` | `127.0.0.1` (Docker: `0.0.0.0`) | listen address for `python -m app` |
| `PORT` | `8080` | listen port for `python -m app` |
| `DATA_DIR` | `./data` (Docker: `/data`) | root of all data, relative to the working directory |
| `DATABASE_URL` | `sqlite:///<DATA_DIR>/jobs.db` | SQLite file: `sqlite:///./x.db`, `sqlite:////abs/x.db` (POSIX), `sqlite:///C:/x.db` (Windows) |
| `UPLOAD_DIR` | `<DATA_DIR>/uploads` | uploaded documents waiting for conversion |
| `RESULT_DIR` | `<DATA_DIR>/results` | generated PDFs |
| `PROFILE_DIR` | `<DATA_DIR>/profiles` | temporary LibreOffice profiles, one per conversion |
| `WORK_DIR` | `<DATA_DIR>/work` | temporary working directories, one per conversion |
| `LIBREOFFICE_BIN` | empty (auto-detect) | LibreOffice executable; if set and invalid, the service is not ready |
| `API_KEYS` | empty (auth disabled) | comma-separated keys accepted in `X-API-Key`, at least 16 characters each |
| `MAX_SYNC_WAIT_SECONDS` | `30` | maximum `wait` for `POST /jobs?wait=N` (0–300); `0` disables single-call mode |
| `MAX_UPLOAD_BYTES` | `26214400` (25 MiB) | larger uploads are rejected with `413` |
| `MAX_WORKERS` | `2` | concurrent conversions: more throughput, more RAM/CPU |
| `MAX_QUEUED_JOBS` | `100` | beyond this many waiting jobs `POST /jobs` returns `503` |
| `MAX_ATTEMPTS` | `2` | maximum attempts for a job interrupted by a restart |
| `CONVERSION_TIMEOUT_SECONDS` | `120` | LibreOffice is killed after this time and the job fails |
| `JOB_TTL_SECONDS` | `3600` | finished jobs and their PDFs are deleted after this time |
| `CLEANUP_INTERVAL_SECONDS` | `60` | cleanup frequency |
| `ZIP_MAX_ENTRIES` | `10000` | maximum number of parts inside a document |
| `ZIP_MAX_UNCOMPRESSED_BYTES` | `536870912` | maximum uncompressed size (zip-bomb protection) |
| `ZIP_MAX_COMPRESSION_RATIO` | `200` | maximum compression ratio for parts larger than 1 MiB |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs the LibreOffice command line |

The four storage directories must be distinct. If you change `DATA_DIR`, leave `DATABASE_URL` empty or update it too.

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | API process is alive (public) |
| `GET` | `/ready` | service can convert: LibreOffice, storage, database, workers (public) |
| `POST` | `/jobs` | create a job (multipart, field `file`) → `202` |
| `POST` | `/jobs?wait=30` | create a job and wait: `200` with the PDF if ready within 30 s, otherwise `202` |
| `GET` | `/jobs?limit=20&offset=0` | paginated job list, newest first (`limit` ≤ 100) |
| `GET` | `/jobs/{job_id}` | job status |
| `GET` | `/jobs/{job_id}/download` | the PDF (`application/pdf`, `attachment`) |
| `DELETE` | `/jobs/{job_id}` | delete the job and its files → `204` |

Job states: `queued` → `processing` → `completed` | `failed`.

### Authentication

When `API_KEYS` is set, every `/jobs...` endpoint requires the `X-API-Key` header; `/health`, `/ready` and `/docs`
stay public. Requests without a valid key get `401` (`code: unauthorized`) and their body is never read.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"     # generate a key
```

```dotenv
API_KEYS=key-for-app-A,key-for-app-B
```

- Use one key per client application, or old and new key together while **rotating** keys.
- Keys are compared in constant time and never logged.
- All keys have the same permissions and see the same jobs; use separate instances to isolate tenants.
- Keys travel in clear text over HTTP: use HTTPS outside localhost.
- In Swagger UI click **Authorize** and paste the key.
- With an empty `API_KEYS` a warning is logged at startup; only acceptable when the service is reachable
  exclusively by the backend that calls it.

### Single-call conversion (`wait`)

With `?wait=N` (N ≤ `MAX_SYNC_WAIT_SECONDS`) the request stays open for at most N seconds and returns as soon as
the conversion ends:

| Outcome within N seconds | Response |
|---|---|
| completed | `200`, body is the **PDF**, `Content-Disposition: attachment`, headers `X-Job-Id` and `Content-Location` |
| failed | `422`, `{"detail": "...", "code": "conversion_failed", "job_id": "...", "status_url": "..."}` |
| still queued / processing | `202` with the job, exactly like without `wait`: continue polling |

```bash
curl -s -X POST "http://127.0.0.1:8080/jobs?wait=30" -H "X-API-Key: $API_KEY" \
     -F "file=@report.docx" -o report.pdf -w "%{http_code} %{content_type}\n"
# 200 application/pdf  -> report.pdf is the PDF
# 202 application/json -> report.pdf contains the job: poll its status_url
```

Waiting requests do not occupy conversion workers, but they are open HTTP connections: client and reverse-proxy
timeouts (e.g. nginx `proxy_read_timeout`) must be longer than N. The job remains available afterwards; call
`DELETE /jobs/{X-Job-Id}` if you do not need it anymore.

### Asynchronous flow with curl

Add `-H "X-API-Key: $API_KEY"` to every call when authentication is enabled.

```bash
curl -X POST http://127.0.0.1:8080/jobs -F "file=@report.docx"
```

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",
  "original_filename": "report.docx",
  "status_url": "/jobs/550e8400-e29b-41d4-a716-446655440000",
  "download_url": null,
  "created_at": "2026-10-23T09:30:00.123456Z",
  "updated_at": "2026-10-23T09:30:00.123456Z",
  "expires_at": null,
  "input_size": 12873,
  "result_size": null,
  "error": null
}
```

```bash
curl http://127.0.0.1:8080/jobs/550e8400-e29b-41d4-a716-446655440000             # status
curl -OJ http://127.0.0.1:8080/jobs/550e8400-e29b-41d4-a716-446655440000/download # saves report.pdf
curl -X DELETE http://127.0.0.1:8080/jobs/550e8400-e29b-41d4-a716-446655440000    # delete
```

When completed, the status contains `"status": "completed"`, a `download_url`, `result_size` and `expires_at`.

Complete bash script:

```bash
JOB=$(curl -s -X POST http://127.0.0.1:8080/jobs -F "file=@report.docx" | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
until curl -s http://127.0.0.1:8080/jobs/$JOB | grep -qE '"status":"(completed|failed)"'; do sleep 1; done
curl -s -o report.pdf http://127.0.0.1:8080/jobs/$JOB/download
```

Windows PowerShell (use `curl.exe`, not the `curl` alias):

```powershell
$job = curl.exe -s -X POST http://127.0.0.1:8080/jobs -F "file=@report.docx" | ConvertFrom-Json
do { Start-Sleep 1; $s = Invoke-RestMethod "http://127.0.0.1:8080$($job.status_url)" } while ($s.status -in "queued","processing")
if ($s.status -eq "completed") { curl.exe -s -o report.pdf "http://127.0.0.1:8080$($s.download_url)" } else { $s.error }
```

## Client examples

Both examples first try the single call (`?wait=30`) and fall back to polling for slow documents. The API key
is read from the `FOLDPDF_API_KEY` environment variable.

**Python**, standard library only: [`examples/client.py`](examples/client.py)

```bash
export FOLDPDF_API_KEY=your-key          # PowerShell: $env:FOLDPDF_API_KEY="your-key"
python examples/client.py report.docx --url http://127.0.0.1:8080
```

Minimal version with `httpx`:

```python
import time, httpx

client = httpx.Client(base_url="http://127.0.0.1:8080", headers={"X-API-Key": "your-key"}, timeout=60)
with open("report.docx", "rb") as fh:
    r = client.post("/jobs", params={"wait": 30}, files={"file": ("report.docx", fh)})

if r.status_code == 200:                      # PDF ready in a single call
    pdf = r.content
elif r.status_code == 202:                    # slow document: poll
    job = r.json()
    while job["status"] in ("queued", "processing"):
        time.sleep(1)
        job = client.get(job["status_url"]).json()
    if job["status"] == "failed":
        raise RuntimeError(job["error"])
    pdf = client.get(job["download_url"]).raise_for_status().content
else:                                         # 4xx/5xx, e.g. 422 conversion_failed
    raise RuntimeError(r.json()["detail"])

open("report.pdf", "wb").write(pdf)
```

**Node.js 18+**, no dependencies: [`examples/client.mjs`](examples/client.mjs)

```bash
FOLDPDF_API_KEY=your-key node examples/client.mjs report.docx http://127.0.0.1:8080
```

## Integrating it into an application

FoldPDF is an **internal service**: end users should not call it directly and the API key must never
reach the browser. Your application backend calls it and takes care of login, permissions and storage.

```text
Browser ──► Your backend (login, permissions, storage) ──X-API-Key──► FoldPDF (internal network)
```

Example: a "Download as PDF" button for a stored DOCX, implemented in a FastAPI backend. The PDF is kept in your
own storage because FoldPDF only keeps it for `JOB_TTL_SECONDS`.

```python
import httpx
from fastapi import Depends, HTTPException, Response

converter = httpx.AsyncClient(base_url="http://127.0.0.1:8080",
                              headers={"X-API-Key": CONVERTER_API_KEY}, timeout=40)

@app.get("/documents/{doc_id}/pdf")
async def download_pdf(doc_id: str, user=Depends(current_user)):
    doc = load_authorized_document(doc_id, user)            # permission check + read the docx
    pdf = pdf_cache.get(doc.sha256)                         # converted before?
    if pdf is None:
        r = await converter.post("/jobs", params={"wait": 30}, files={"file": (doc.name, doc.content)})
        if r.status_code == 202:
            raise HTTPException(503, "PDF is being prepared, retry shortly", headers={"Retry-After": "10"})
        if r.status_code != 200:
            raise HTTPException(502, "Conversion failed")
        pdf = r.content
        await converter.delete(f"/jobs/{r.headers['X-Job-Id']}")   # free the converter right away
        pdf_cache.set(doc.sha256, pdf)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{doc.pdf_name}"'})
```

Recommended variants:

- **Convert ahead of time**: generate the PDF in the background when the DOCX is uploaded, so it is ready on click.
- **Large documents**: use the asynchronous flow (`202` + polling) from a backend worker and notify the user.
- Honour `Retry-After` on `503` responses (`queue_full`, `engine_unavailable`).
- Multiple FoldPDF instances each have their own queue and data: query the instance that created the job.

## HTTP status codes and errors

Errors always look like `{"detail": "<readable message>", "code": "<stable code>"}`. They never include internal
paths, stack traces or LibreOffice output, which only go to the server logs. Use `code`
for programmatic handling: messages may change, codes are stable.

| HTTP | `code` | When |
|---|---|---|
| 200 | — | successful read / PDF returned (also from `POST /jobs?wait=N` when ready in time) |
| 202 | — | job created; `Location` header points to the status URL |
| 204 | — | job deleted |
| 400 | `empty_file` | empty upload |
| 400 | `invalid_document` | corrupted, not ZIP/OOXML, encrypted, content not matching the extension, zip bomb |
| 401 | `unauthorized` | `API_KEYS` set and `X-API-Key` missing or wrong |
| 404 | `job_not_found` | unknown, deleted or expired job |
| 409 | `job_not_ready` | download requested before completion or for a failed job |
| 409 | `job_in_progress` | deleting a job that is being converted |
| 410 | `result_unavailable` | job completed but the PDF is no longer on disk |
| 413 | `file_too_large` | upload larger than `MAX_UPLOAD_BYTES` |
| 415 | `unsupported_file_type` | extension other than `.docx`, `.xlsx`, `.pptx` |
| 422 | `validation_error` | missing `file` field or invalid parameters (e.g. `wait` above the limit) |
| 422 | `conversion_failed` | with `wait`: the conversion failed within the waiting time (includes `job_id`) |
| 500 | `internal_error` | unexpected error (details in the logs only) |
| 503 | `engine_unavailable` | LibreOffice not available (`Retry-After: 30`) |
| 503 | `queue_full` | too many waiting jobs (`Retry-After: 30`) |
| 503 | `service_unavailable` | service is shutting down |

## Architecture

```text
            ┌──────────────────────────── single process (uvicorn) ──────────────────────────┐
 client ──► │ API key middleware ─► FastAPI routes (app/api)                                 │
            │   │ chunked upload + ZIP/OOXML validation ──► file in UPLOAD_DIR (UUID name)   │
            │   ▼                                                                            │
            │ JobManager (app/jobs/manager.py) ── SQLite (app/jobs/repository.py)            │
            │   │ asyncio.Queue + MAX_WORKERS workers                                        │
            │   ▼ asyncio.to_thread                                                          │
            │ LibreOfficeConverter (app/services/converter.py)                               │
            │   └─► soffice --headless --convert-to pdf  (dedicated profile and workdir)     │
            │          └─► PDF in RESULT_DIR ──► download or direct response (wait)          │
            │ Periodic cleanup (TTL, orphaned files)                                         │
            └────────────────────────────────────────────────────────────────────────────────┘
```

| Module | Responsibility |
|---|---|
| `app/config.py` | centralized configuration from environment / `.env`, validated at startup |
| `app/api/auth.py` | API key middleware and OpenAPI security scheme |
| `app/api/routes.py` | REST endpoints, no conversion logic |
| `app/api/errors.py` | safe JSON errors, request body size limit |
| `app/services/validation.py` | file names, extensions, ZIP and Office Open XML structure |
| `app/services/storage.py` | directories, UUID file names, permissions, robust deletion (Windows included) |
| `app/services/libreoffice.py` | locating and probing the LibreOffice executable |
| `app/services/converter.py` | isolated conversion with timeout, process-tree kill and PDF checks |
| `app/jobs/repository.py` | SQLite persistence with atomic state transitions |
| `app/jobs/manager.py` | queue, workers, restart recovery, download leases, waiting requests, cleanup |

**Why an in-process queue.** The service uses a local queue (asyncio + threads) instead of Redis, RabbitMQ or
Celery, so the native and Docker setups are identical and need no extra services. SQLite is the source of truth
and the in-memory queue is rebuilt from `queued` jobs at startup. Concurrency is bounded by `MAX_WORKERS`, and
each conversion is a separate LibreOffice process.

Limits compared to a distributed queue:

- run **one process only** (no `uvicorn --workers N`, a single container replica): several processes would have
  separate queues over the same database;
- no horizontal scaling or load balancing across machines;
- queued jobs survive restarts but not the loss of the data disk.

**Restart recovery.** Jobs left in `processing` after a crash are re-queued if their upload still exists and they
have not exceeded `MAX_ATTEMPTS`; otherwise they are marked `failed`. No job stays in `processing` forever.

**Concurrency safety.** State transitions are atomic `UPDATE ... WHERE status = ?` statements: a job deleted while
queued is never converted and a running job cannot be deleted (`409`). Every download holds a lease, so deletion
and cleanup postpone removing the PDF until the HTTP response is complete.

## Tests

- **Unit tests** (`tests/test_*.py`) do not need LibreOffice: the external process is mocked.
- **Integration tests** (`tests/integration/`, marker `integration`) convert real DOCX, XLSX and PPTX files with
  LibreOffice, including the single-call mode. They are skipped when LibreOffice is not installed.

```bash
python -m pytest                       # everything (integration skipped without LibreOffice)
python -m pytest -m "not integration"  # unit tests only
python -m pytest -m integration -v     # integration only (set LIBREOFFICE_BIN if needed)
```

Inside Docker (unit + integration with real LibreOffice):

```bash
docker build --target test -t foldpdf:test .
docker run --rm foldpdf:test
```

With `make` (Linux/macOS): `make install`, `make test`, `make test-integration`, `make docker-test`.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `/ready` returns `503`, *LibreOffice was not found* | install LibreOffice or set `LIBREOFFICE_BIN` to the full path of `soffice`/`soffice.exe` |
| *LIBREOFFICE_BIN does not point to a valid LibreOffice executable* | check the path; no quotes needed on Windows even with spaces |
| *LibreOffice is installed but does not work properly* | run `soffice --headless --version` manually; on Linux install the `-nogui` packages |
| `401 unauthorized` | send `X-API-Key` with one of the keys in `API_KEYS` |
| Jobs fail with a timeout | very large document or slow machine: raise `CONVERSION_TIMEOUT_SECONDS` |
| `wait` always returns `202` | conversion takes longer than `wait`, or a proxy closes the connection: raise `wait` and proxy timeouts |
| `413` below the configured limit | a reverse proxy has its own limit (nginx `client_max_body_size`) |
| Slow conversions or out of memory | lower `MAX_WORKERS` |
| Port already in use | change `PORT` / `--port` (Docker: `FOLDPDF_PORT`) |
| Docker `permission denied` on a bind-mounted `/data` | make the host directory writable by UID 10001 or use the named volume |
| Fonts substituted in the PDF | install the fonts used by the document (see below) |

Set `LOG_LEVEL=DEBUG` to log the LibreOffice command line and, on failure, its (truncated) output.

## Layout fidelity and limitations

Conversions are done by LibreOffice, not Microsoft Office: results are usually good but **not always identical**.

- **Fonts**: missing fonts are substituted and line/page breaks may change. Carlito and Caladea are metric-compatible
  with Calibri and Cambria, Liberation with Arial, Times New Roman and Courier New. Install other fonts
  (e.g. Aptos, Segoe) on the host or in the Docker image.
- **Word**: SmartArt, some fields, complex text boxes, watermarks and some table styles may differ.
- **Excel**: print area, scaling and orientation are honoured when set in the file; without a print area wide sheets
  may span many pages. Complex charts and advanced conditional formatting may differ; macros and external data
  connections are not executed.
- **PowerPoint**: animations and transitions cannot be represented in PDF; some effects and embedded media are lost.
- Newer LibreOffice versions generally improve compatibility.

## Security, persistence and retention

**Security**

- Documents are **untrusted input**. Validation (extension, ZIP integrity with CRC, `[Content_Types].xml` parsed with
  `defusedxml`, main part consistent with the format, zip-bomb limits, suspicious internal paths and encrypted
  files rejected) reduces the risk but **does not make a document safe**: LibreOffice still parses it, so keep
  LibreOffice up to date.
- The client-declared extension and MIME type are never trusted on their own.
- Files on disk get random UUID names; the original name is only used in JSON responses and in the sanitized,
  RFC 5987-encoded `Content-Disposition` header.
- LibreOffice is started without a shell, with separate arguments, a dedicated profile and working directory per
  job, a timeout and process-tree termination.
- Responses never contain internal paths, stack traces or LibreOffice logs.
- Uploads are read in chunks; oversized `Content-Length` bodies are rejected before being read and chunked bodies
  are cut off as soon as the limit is exceeded.
- API key authentication is service-to-service, not end-user authentication. Do not expose the service to the
  internet without a reverse proxy providing TLS and rate limiting.

**Privileges**

- *Docker*: non-root user (UID 10001), read-only root filesystem, no Linux capabilities, `no-new-privileges`.
- *Native*: the process (and LibreOffice) **inherits the privileges of the user who starts it**. Use a dedicated
  unprivileged user (see the systemd unit). On POSIX systems data directories are created with mode `0700` and
  files with `0600`; on Windows the folder ACLs apply, so put `DATA_DIR` somewhere **not accessible to other users**.

**Persistence and retention**

- Metadata in SQLite (WAL mode), files in separate directories; database and files stay consistent across restarts.
- PDFs are **not** deleted right after conversion or download: they remain available for `JOB_TTL_SECONDS` after
  completion, then the periodic cleanup removes job and files. `DELETE /jobs/{id}` removes them immediately.
- Uploaded documents are deleted as soon as the conversion ends, successfully or not.
- Cleanup never touches queued or running jobs or files being downloaded, and only removes files whose names
  were generated by the service.
- Backup: stop the service and copy `DATA_DIR` (or the Docker volume `foldpdf-data`).

## Contributing

1. Open an issue to discuss significant changes.
2. Create a branch and add tests for every feature or fix.
3. Make sure `python -m pytest` passes (and, if possible, `docker build --target test` + `docker run`).
4. Keep the native and Docker modes equivalent: no Docker-only or native-only features.
5. Open a pull request describing the change.

Please report security issues privately to the maintainers instead of opening a public issue.

## License

The original code of this project is released under the **MIT License** (see [`LICENSE`](LICENSE)).

LibreOffice, Python, the fonts and the other components installed natively or bundled in the Docker image keep
**their own licenses** (MPL-2.0, LGPL, GPL, OFL, PSF, ...). Whoever distributes the Docker image must comply with
them, including making source code available where required: see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
