"""Application exceptions carrying messages that are safe to return to the client.

Each exception holds the HTTP status code, a stable machine-readable code and a
readable message that never contains internal paths or system details.
"""

from __future__ import annotations


class ServiceError(Exception):
    status_code: int = 500
    code: str = "internal_error"
    message: str = "Internal server error."

    def __init__(
        self,
        message: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.message = message or type(self).message
        self.headers = headers
        self.extra = extra or {}
        super().__init__(self.message)


class UnauthorizedError(ServiceError):
    status_code = 401
    code = "unauthorized"
    message = "Missing or invalid API key. Send it in the X-API-Key header."


class InvalidParameterError(ServiceError):
    status_code = 422
    code = "validation_error"
    message = "Invalid parameter."


class JobFailedError(ServiceError):
    status_code = 422
    code = "conversion_failed"
    message = "The conversion failed."


class UnsupportedFileTypeError(ServiceError):
    status_code = 415
    code = "unsupported_file_type"
    message = "Unsupported format. Only .docx, .xlsx and .pptx files are accepted."


class EmptyUploadError(ServiceError):
    status_code = 400
    code = "empty_file"
    message = "The uploaded file is empty."


class UploadTooLargeError(ServiceError):
    status_code = 413
    code = "file_too_large"
    message = "The file exceeds the maximum allowed size."


class InvalidDocumentError(ServiceError):
    status_code = 400
    code = "invalid_document"
    message = "The file is not a valid Office Open XML document or it is corrupted."


class JobNotFoundError(ServiceError):
    status_code = 404
    code = "job_not_found"
    message = "Job not found."


class JobNotReadyError(ServiceError):
    status_code = 409
    code = "job_not_ready"
    message = "The PDF is not available yet."


class JobInProgressError(ServiceError):
    status_code = 409
    code = "job_in_progress"
    message = "The job is being processed and cannot be deleted right now."


class ResultUnavailableError(ServiceError):
    status_code = 410
    code = "result_unavailable"
    message = "The PDF is no longer available."


class EngineUnavailableError(ServiceError):
    status_code = 503
    code = "engine_unavailable"
    message = "The LibreOffice conversion engine is not available."


class QueueFullError(ServiceError):
    status_code = 503
    code = "queue_full"
    message = "Too many queued jobs. Please retry later."


class ServiceUnavailableError(ServiceError):
    status_code = 503
    code = "service_unavailable"
    message = "The service is not accepting new jobs right now."
