"""POST /jobs?wait=N: the PDF in the same response when the conversion ends within N seconds."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import MINIMAL_PDF, FakeConverter, FakeLocator, make_settings, wait_for_status
from tests.ooxml_factory import make_docx, make_pptx


def _post(client: TestClient, wait: int | None, data: bytes | None = None, name: str = "report.docx"):  # type: ignore[no-untyped-def]
    url = "/jobs" if wait is None else f"/jobs?wait={wait}"
    return client.post(url, files={"file": (name, data or make_docx(), "application/octet-stream")})


def test_pdf_returned_directly_when_fast(client: TestClient) -> None:
    response = _post(client, 10)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith('attachment; filename="report.pdf"')
    assert response.content == MINIMAL_PDF
    job_id = response.headers["X-Job-Id"]
    assert response.headers["Content-Location"] == f"/jobs/{job_id}/download"
    # the job can still be queried and downloaded until it expires or is deleted
    assert client.get(f"/jobs/{job_id}").json()["status"] == "completed"
    assert client.get(f"/jobs/{job_id}/download").content == MINIMAL_PDF
    assert client.delete(f"/jobs/{job_id}").status_code == 204


def test_other_formats_in_wait_mode(client: TestClient) -> None:
    response = _post(client, 10, make_pptx(), "slide.pptx")
    assert response.status_code == 200
    assert 'filename="slide.pdf"' in response.headers["content-disposition"]


def test_returns_202_when_conversion_is_slower_than_wait(client: TestClient, fake_converter: FakeConverter) -> None:
    fake_converter.gate = threading.Event()
    started = time.monotonic()
    response = _post(client, 1)
    elapsed = time.monotonic() - started
    assert response.status_code == 202
    body = response.json()
    assert body["status"] in {"queued", "processing"}
    assert response.headers["Location"] == body["status_url"]
    assert 0.9 <= elapsed < 5
    fake_converter.gate.set()
    assert wait_for_status(client, body["job_id"])["status"] == "completed"


def test_returns_as_soon_as_done_not_after_full_wait(client: TestClient) -> None:
    started = time.monotonic()
    assert _post(client, 30).status_code == 200
    assert time.monotonic() - started < 5


def test_failed_conversion_returns_422(client: TestClient, fake_converter: FakeConverter) -> None:
    fake_converter.mode = "fail"
    response = _post(client, 10)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "conversion_failed"
    assert body["detail"] == "LibreOffice could not convert the document."
    assert body["status_url"] == f"/jobs/{body['job_id']}"
    assert client.get(body["status_url"]).json()["status"] == "failed"


def test_timeout_in_wait_mode(client: TestClient, fake_converter: FakeConverter) -> None:
    fake_converter.mode = "timeout"
    response = _post(client, 10)
    assert response.status_code == 422
    assert "maximum time" in response.json()["detail"]


def test_wait_zero_or_missing_keeps_async_behaviour(client: TestClient) -> None:
    for wait in (None, 0):
        response = _post(client, wait)
        assert response.status_code == 202
        assert response.json()["status"] in {"queued", "processing", "completed"}


def test_wait_above_limit_is_rejected(client: TestClient, settings) -> None:  # type: ignore[no-untyped-def]
    response = _post(client, settings.max_sync_wait_seconds + 1)
    assert response.status_code == 422
    assert str(settings.max_sync_wait_seconds) in response.json()["detail"]
    assert list(settings.upload_dir.iterdir()) == []


def test_negative_wait_is_rejected(client: TestClient) -> None:
    assert _post(client, -1).status_code == 422


def test_validation_errors_still_apply_in_wait_mode(client: TestClient) -> None:
    assert _post(client, 10, b"not a docx").status_code == 400
    assert _post(client, 10, make_docx(), "file.txt").status_code == 415


def test_waiting_requests_while_queue_is_busy(tmp_path: Path) -> None:
    """With the only worker busy, a waiting request gets its PDF as soon as its turn completes."""
    converter = FakeConverter()
    converter.gate = threading.Event()
    app = create_app(make_settings(tmp_path, max_workers=1), converter=converter, locator=FakeLocator())  # type: ignore[arg-type]
    with TestClient(app) as client:
        blocking = _post(client, None).json()["job_id"]
        assert converter.started.wait(5)
        threading.Timer(0.5, converter.gate.set).start()
        response = _post(client, 10)
        assert response.status_code == 200
        assert response.content == MINIMAL_PDF
        assert wait_for_status(client, blocking)["status"] == "completed"


def test_disabled_sync_wait(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, max_sync_wait_seconds=0), converter=FakeConverter(), locator=FakeLocator())  # type: ignore[arg-type]
    with TestClient(app) as client:
        assert _post(client, 1).status_code == 422
        assert _post(client, 0).status_code == 202
