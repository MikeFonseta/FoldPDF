"""REST endpoints. Job logic lives in `app.jobs.manager`, conversion in `app.services`."""

from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.types import Receive, Scope, Send

from app.api.auth import require_api_key
from app.config import Settings
from app.exceptions import InvalidParameterError, JobFailedError
from app.jobs.manager import DownloadLease, JobManager
from app.jobs.repository import JobStatus
from app.schemas import ErrorResponse, HealthResponse, JobListResponse, JobResponse, ReadinessResponse
from app.services.storage import Storage, remove_file
from app.services.validation import ZipLimits, get_extension, sanitize_filename, validate_ooxml

# Public endpoints (health checks) and job endpoints, protected by API key when configured.
router = APIRouter()
jobs_router = APIRouter(tags=["jobs"], dependencies=[Depends(require_api_key)])


def _manager(request: Request) -> JobManager:
    return request.app.state.manager


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _storage(request: Request) -> Storage:
    return request.app.state.storage


def _base_path(request: Request) -> str:
    return str(request.scope.get("root_path", "")).rstrip("/")


ManagerDep = Annotated[JobManager, Depends(_manager)]

_PDF_CONTENT = {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}


def _errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    descriptions = {
        400: "Empty or corrupted file, or content not matching the declared format.",
        401: "Missing or invalid API key.",
        404: "Job not found.",
        409: "Operation not allowed in the current job state.",
        410: "The PDF is no longer available.",
        413: "File too large.",
        415: "Unsupported extension.",
        422: "Invalid parameters or, with `wait`, failed conversion (code=conversion_failed).",
        503: "Conversion engine unavailable or queue full.",
    }
    return {code: {"model": ErrorResponse, "description": descriptions[code]} for code in codes}


def content_disposition(filename: str) -> str:
    """`attachment` header with an ASCII fallback and a UTF-8 name per RFC 6266/5987."""
    fallback = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    fallback = "".join(ch for ch in fallback if ch.isprintable() and ch not in '"\\;') or "document.pdf"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


class LeasedFileResponse(FileResponse):
    """FileResponse that releases the download lease even if the client disconnects."""

    def __init__(self, *args: Any, on_close: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._on_close()


def _pdf_response(lease: DownloadLease, extra_headers: dict[str, str] | None = None) -> FileResponse:
    try:
        return LeasedFileResponse(
            lease.path,
            media_type="application/pdf",
            headers={
                "Content-Disposition": content_disposition(lease.filename),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                **(extra_headers or {}),
            },
            on_close=lease.release,
        )
    except BaseException:
        lease.release()
        raise


@router.get("/health", response_model=HealthResponse, tags=["health"], summary="The API process is alive")
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    tags=["health"],
    summary="The service is ready to convert",
    responses={503: {"model": ReadinessResponse, "description": "Service not ready."}},
)
async def ready(manager: ManagerDep) -> JSONResponse:
    readiness = await asyncio.to_thread(manager.readiness)
    body = ReadinessResponse(
        status="ready" if readiness.ready else "not_ready",
        checks=readiness.checks,
        libreoffice_version=readiness.libreoffice_version,
        detail=readiness.detail,
    )
    return JSONResponse(body.model_dump(), status_code=200 if readiness.ready else 503)


@jobs_router.post(
    "/jobs",
    status_code=202,
    response_model=JobResponse,
    summary="Create a conversion job (optionally waiting for the PDF)",
    description=(
        "Without `wait` it answers `202` immediately with the job to poll.\n\n"
        "With `wait=N` (seconds, at most `MAX_SYNC_WAIT_SECONDS`) the request waits for the conversion:\n"
        "- completed within N seconds → `200` with the **PDF** as body (header `X-Job-Id`);\n"
        "- failed within N seconds → `422` with `code=conversion_failed`;\n"
        "- still running → `202` with the job, continue by polling."
    ),
    responses={
        200: {"content": _PDF_CONTENT, "description": "PDF ready within the `wait` time."},
        **_errors(400, 401, 413, 415, 422, 503),
    },
)
async def create_job(
    request: Request,
    response: Response,
    manager: ManagerDep,
    file: Annotated[UploadFile, File(description=".docx, .xlsx or .pptx document")],
    wait: Annotated[
        int, Query(ge=0, description="Seconds to wait for the PDF in the same request (0 = do not wait)")
    ] = 0,
) -> Any:
    settings = _settings(request)
    try:
        if wait > settings.max_sync_wait_seconds:
            raise InvalidParameterError(
                f"The wait parameter cannot exceed {settings.max_sync_wait_seconds} seconds."
            )
        manager.ensure_accepting()
        original_filename = sanitize_filename(file.filename)
        extension = get_extension(original_filename)
        upload_path, size = await _storage(request).receive_upload(file, settings.max_upload_bytes)
        try:
            limits = ZipLimits(
                settings.zip_max_entries, settings.zip_max_uncompressed_bytes, settings.zip_max_compression_ratio
            )
            await asyncio.to_thread(validate_ooxml, upload_path, extension, limits)
            job = manager.submit(upload_path, original_filename, extension, size, notify=wait > 0)
        except BaseException:
            remove_file(upload_path)
            raise
    finally:
        await file.close()

    base_path = _base_path(request)
    if wait > 0:
        job = await manager.wait_for_job(job.id, wait)
        status_url = f"{base_path}/jobs/{job.id}"
        if job.status == JobStatus.COMPLETED:
            lease = manager.open_download(job.id)
            return _pdf_response(lease, {"X-Job-Id": job.id, "Content-Location": f"{status_url}/download"})
        if job.status == JobStatus.FAILED:
            raise JobFailedError(job.error_message, extra={"job_id": job.id, "status_url": status_url})

    body = JobResponse.from_record(job, base_path)
    response.headers["Location"] = body.status_url
    return body


@jobs_router.get("/jobs", response_model=JobListResponse, summary="List jobs", responses=_errors(401))
async def list_jobs(
    request: Request,
    manager: ManagerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobListResponse:
    jobs, total = manager.list_jobs(limit, offset)
    base = _base_path(request)
    return JobListResponse(
        items=[JobResponse.from_record(job, base) for job in jobs], total=total, limit=limit, offset=offset
    )


@jobs_router.get(
    "/jobs/{job_id}", response_model=JobResponse, summary="Job status", responses=_errors(401, 404)
)
async def get_job(request: Request, job_id: str, manager: ManagerDep) -> JobResponse:
    return JobResponse.from_record(manager.get_job(job_id), _base_path(request))


@jobs_router.get(
    "/jobs/{job_id}/download",
    response_class=FileResponse,
    summary="Download the PDF",
    responses={200: {"content": _PDF_CONTENT, "description": "The generated PDF."}, **_errors(401, 404, 409, 410)},
)
async def download_job(job_id: str, manager: ManagerDep) -> FileResponse:
    return _pdf_response(manager.open_download(job_id))


@jobs_router.delete(
    "/jobs/{job_id}",
    status_code=204,
    summary="Delete a job and its files",
    responses=_errors(401, 404, 409),
)
async def delete_job(job_id: str, manager: ManagerDep) -> Response:
    manager.delete_job(job_id)
    return Response(status_code=204)
