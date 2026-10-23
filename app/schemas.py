"""JSON schemas of the REST responses."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from app.jobs.repository import JobRecord, JobStatus


def _ts(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, tz=UTC) if value is not None else None


class JobResponse(BaseModel):
    job_id: str = Field(examples=["550e8400-e29b-41d4-a716-446655440000"])
    status: JobStatus
    original_filename: str = Field(examples=["report.docx"])
    status_url: str = Field(examples=["/jobs/550e8400-e29b-41d4-a716-446655440000"])
    download_url: str | None = Field(default=None, examples=[None])
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    input_size: int
    result_size: int | None = None
    error: str | None = None

    @classmethod
    def from_record(cls, job: JobRecord, base_path: str = "") -> JobResponse:
        status_url = f"{base_path}/jobs/{job.id}"
        return cls(
            job_id=job.id,
            status=job.status,
            original_filename=job.original_filename,
            status_url=status_url,
            download_url=f"{status_url}/download" if job.status == JobStatus.COMPLETED else None,
            created_at=_ts(job.created_at),
            updated_at=_ts(job.updated_at),
            expires_at=_ts(job.expires_at),
            input_size=job.input_size,
            result_size=job.result_size,
            error=job.error_message,
        )


class JobListResponse(BaseModel):
    items: list[JobResponse]
    total: int
    limit: int
    offset: int


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok"])


class ReadinessResponse(BaseModel):
    status: str = Field(examples=["ready"])
    checks: dict[str, bool]
    libreoffice_version: str | None = None
    detail: str


class ErrorResponse(BaseModel):
    detail: str
    code: str
    job_id: str | None = Field(default=None, description="Only present for code=conversion_failed")
    status_url: str | None = Field(default=None, description="Only present for code=conversion_failed")
