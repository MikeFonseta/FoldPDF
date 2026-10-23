"""Integration tests with real LibreOffice.

Run with: `pytest -m integration`. They are skipped when LibreOffice is not
installed (or `LIBREOFFICE_BIN` does not point to a valid executable).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.libreoffice import find_libreoffice
from tests.conftest import make_settings, upload, wait_for_status
from tests.ooxml_factory import SAMPLES

pytestmark = pytest.mark.integration

# Read at import time, before the isolation fixture clears the environment.
LIBREOFFICE_BIN = os.environ.get("LIBREOFFICE_BIN") or None
LIBREOFFICE = find_libreoffice(LIBREOFFICE_BIN)

requires_libreoffice = pytest.mark.skipif(LIBREOFFICE is None, reason="LibreOffice is not installed")


@pytest.fixture(scope="module")
def real_client(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    settings = make_settings(
        tmp_path_factory.mktemp("integration"),
        libreoffice_bin=str(LIBREOFFICE),
        conversion_timeout_seconds=180,
        max_sync_wait_seconds=180,
        max_upload_bytes=10 * 1024 * 1024,
    )
    with TestClient(create_app(settings)) as client:
        yield client


@requires_libreoffice
def test_ready_with_real_engine(real_client: TestClient) -> None:
    response = real_client.get("/ready")
    assert response.status_code == 200, response.json()
    assert response.json()["checks"]["libreoffice"] is True


@requires_libreoffice
@pytest.mark.parametrize("extension", [".docx", ".xlsx", ".pptx"])
def test_real_conversion(real_client: TestClient, extension: str, tmp_path: Path) -> None:
    text = f"Real conversion {extension}"
    response = upload(real_client, SAMPLES[extension](text), f"integration{extension}")
    assert response.status_code == 202, response.json()

    final = wait_for_status(real_client, response.json()["job_id"], timeout=240)
    assert final["status"] == "completed", final

    download = real_client.get(final["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"
    pdf = download.content
    assert pdf.startswith(b"%PDF-")
    assert b"%%EOF" in pdf[-2048:]
    assert re.search(rb"/Type\s*/Page\b", pdf), "the PDF contains no pages"
    assert len(pdf) == final["result_size"]
    (tmp_path / f"output{extension}.pdf").write_bytes(pdf)


@requires_libreoffice
@pytest.mark.parametrize("extension", [".docx", ".xlsx", ".pptx"])
def test_real_conversion_in_single_call(real_client: TestClient, extension: str) -> None:
    response = real_client.post(
        "/jobs?wait=180",
        files={"file": (f"single-call{extension}", SAMPLES[extension](f"Single call {extension}"), "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    assert re.search(rb"/Type\s*/Page\b", response.content)
    assert real_client.delete(f"/jobs/{response.headers['X-Job-Id']}").status_code == 204
