"""FastAPI entry point, shared by the native mode and Docker.

Start with `uvicorn app.main:app --host 127.0.0.1 --port 8080` or `python -m app`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.auth import ApiKeyMiddleware
from app.api.errors import MULTIPART_OVERHEAD_BYTES, UploadSizeLimitMiddleware, register_error_handlers
from app.api.routes import jobs_router, router
from app.config import Settings, get_settings
from app.jobs.manager import Converter, JobManager
from app.jobs.repository import JobRepository
from app.logging_config import configure_logging
from app.services.converter import LibreOfficeConverter
from app.services.libreoffice import LibreOfficeLocator
from app.services.storage import Storage

logger = logging.getLogger("app")

DESCRIPTION = """
Asynchronous REST service that converts **DOCX**, **XLSX** and **PPTX** documents to **PDF**
using headless LibreOffice.

Asynchronous flow: `POST /jobs` (202 + `job_id`) → `GET /jobs/{job_id}` until `completed` →
`GET /jobs/{job_id}/download`.

Single-call flow: `POST /jobs?wait=30` returns the PDF directly when the conversion finishes
within 30 seconds, otherwise `202` as in the asynchronous flow.

When `API_KEYS` is configured, the `/jobs` endpoints require the `X-API-Key` header.
"""


def create_app(
    settings: Settings | None = None,
    *,
    converter: Converter | None = None,
    locator: LibreOfficeLocator | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    storage = Storage(settings)
    repository = JobRepository(settings.database_path)
    locator = locator or LibreOfficeLocator(settings.libreoffice_bin, settings.profile_dir)
    converter = converter or LibreOfficeConverter(
        locator, settings.work_dir, settings.profile_dir, settings.conversion_timeout_seconds
    )
    manager = JobManager(settings, repository, storage, converter, locator)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        logger.info("Starting FoldPDF %s (data in %s)", __version__, settings.data_dir)
        if settings.api_key_list:
            logger.info("API key authentication enabled (%d keys configured)", len(settings.api_key_list))
        else:
            logger.warning("Authentication DISABLED (API_KEYS is empty): anyone reaching the service can use /jobs")
        storage.ensure_dirs()
        repository.init()
        engine = await asyncio.to_thread(locator.detect)
        await manager.start()
        if engine.available:
            logger.info("API process started: service ready to process conversions")
        else:
            logger.error(
                "API process started but the LibreOffice engine is NOT available: "
                "new conversions will be rejected with 503 until the problem is fixed"
            )
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(title="FoldPDF", version=__version__, description=DESCRIPTION, lifespan=lifespan)
    app.state.settings = settings
    app.state.storage = storage
    app.state.manager = manager
    register_error_handlers(app)
    app.add_middleware(UploadSizeLimitMiddleware, max_body_bytes=settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES)
    # Added last = runs first: unauthenticated requests never get to read the body.
    app.add_middleware(ApiKeyMiddleware, valid_keys=settings.api_key_list)
    app.include_router(router)
    app.include_router(jobs_router)
    return app


app = create_app()
