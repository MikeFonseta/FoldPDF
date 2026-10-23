"""Exception handlers and request body size limit, with safe JSON responses."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.exceptions import ServiceError, UploadTooLargeError

logger = logging.getLogger(__name__)

# Allowance for the multipart headers on top of the maximum file size.
MULTIPART_OVERHEAD_BYTES = 64 * 1024

_HTTP_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    413: "file_too_large",
    415: "unsupported_media_type",
}


def _error(
    status_code: int,
    detail: str,
    code: str,
    headers: dict[str, str] | None = None,
    extra: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code, **(extra or {})}, status_code=status_code, headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return _error(exc.status_code, exc.message, exc.code, exc.headers, exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Invalid request."
        return _error(exc.status_code, detail, _HTTP_CODES.get(exc.status_code, "http_error"), exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # The received input is never echoed back, only the location and reason of the error.
        errors = [
            {"loc": [str(part) for part in err.get("loc", ())], "msg": str(err.get("msg", ""))}
            for err in exc.errors()
        ]
        return JSONResponse(
            {"detail": "Invalid request.", "code": "validation_error", "errors": errors}, status_code=422
        )

    @app.exception_handler(Exception)
    async def _unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error", exc_info=exc)
        return _error(500, "Internal server error.", "internal_error")


class UploadSizeLimitMiddleware:
    """Reject oversized POST /jobs bodies before they are fully read.

    - with a declared `Content-Length`, the request is rejected immediately with 413;
    - with chunked transfer, reading stops as soon as the limit is exceeded.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    @staticmethod
    def _applies(scope: Scope) -> bool:
        return scope["type"] == "http" and scope["method"] == "POST" and scope["path"].rstrip("/").endswith("/jobs")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._applies(scope):
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.max_body_bytes:
                    response = _error(413, UploadTooLargeError.message, UploadTooLargeError.code)
                    await response(scope, receive, send)
                    return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    # FastAPI re-raises HTTPException even while parsing the form.
                    raise HTTPException(413, UploadTooLargeError.message)
            return message

        await self.app(scope, limited_receive, send)
