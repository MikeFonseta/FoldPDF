from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import FakeConverter, FakeLocator, make_settings, upload
from tests.ooxml_factory import make_docx


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_when_engine_available(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"libreoffice": True, "storage": True, "database": True, "workers": True}
    assert body["libreoffice_version"] == "24.8.0"


def test_not_ready_when_engine_missing(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), converter=FakeConverter(), locator=FakeLocator(available=False))
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        response = client.get("/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["libreoffice"] is False
        assert "LibreOffice" in body["detail"]

        rejected = upload(client, make_docx())
        assert rejected.status_code == 503
        assert rejected.json()["code"] == "engine_unavailable"
        assert rejected.headers["Retry-After"] == "30"


def test_openapi_docs_available(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "FoldPDF"
    assert {"/health", "/ready", "/jobs", "/jobs/{job_id}", "/jobs/{job_id}/download"} <= set(schema["paths"])
