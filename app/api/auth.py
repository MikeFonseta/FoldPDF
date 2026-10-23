"""API key authentication (`X-API-Key` header).

When `API_KEYS` is empty authentication is disabled. When it is enabled:
- `/health`, `/ready` and the documentation stay public (used by health checks);
- every `/jobs...` endpoint requires a valid key.

The check runs in an ASGI middleware, so an unauthenticated request is rejected
with 401 *before* its body is read (no upload is ever stored). The
`require_api_key` dependency repeats the same check and declares it in the
OpenAPI schema, so Swagger UI shows the "Authorize" button.
"""

from __future__ import annotations

import hmac
from collections.abc import Sequence

from fastapi import Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from starlette.types import ASGIApp, Receive, Scope, Send

from app.exceptions import UnauthorizedError

API_KEY_HEADER = "X-API-Key"
_PROTECTED_PREFIX = "/jobs"

api_key_header = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    description="API key configured in API_KEYS (ignored when authentication is disabled).",
)


def is_authorized(valid_keys: Sequence[str], provided: str | None) -> bool:
    if not valid_keys:
        return True
    if not provided:
        return False
    candidate = provided.encode()
    matched = False
    for key in valid_keys:  # no early exit: timing does not depend on which key matches
        matched |= hmac.compare_digest(candidate, key.encode())
    return matched


def _unauthorized() -> UnauthorizedError:
    return UnauthorizedError(headers={"WWW-Authenticate": "ApiKey"})


async def require_api_key(request: Request, api_key: str | None = Security(api_key_header)) -> None:
    if not is_authorized(request.app.state.settings.api_key_list, api_key):
        raise _unauthorized()


class ApiKeyMiddleware:
    def __init__(self, app: ASGIApp, valid_keys: Sequence[str]) -> None:
        self.app = app
        self.valid_keys = tuple(valid_keys)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.valid_keys and scope["type"] == "http" and self._protected(scope):
            header = API_KEY_HEADER.lower().encode()
            provided = next((value for name, value in scope.get("headers", []) if name == header), None)
            if not is_authorized(self.valid_keys, provided.decode("latin-1") if provided else None):
                error = _unauthorized()
                response = JSONResponse(
                    {"detail": error.message, "code": error.code}, status_code=error.status_code, headers=error.headers
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _protected(scope: Scope) -> bool:
        path: str = scope["path"]
        root_path: str = scope.get("root_path", "")
        if root_path and path.startswith(root_path):
            path = path[len(root_path):]
        return path.rstrip("/") == _PROTECTED_PREFIX or path.startswith(_PROTECTED_PREFIX + "/")
