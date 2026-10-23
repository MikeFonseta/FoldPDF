"""API key authentication."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.auth import is_authorized
from app.config import Settings
from app.main import create_app
from tests.conftest import FakeConverter, FakeLocator, make_settings, wait_for_status
from tests.ooxml_factory import make_docx

KEY_A = "client-key-a-0123456789"
KEY_B = "client-key-b-9876543210"


@pytest.fixture
def secured_settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, api_keys=f" {KEY_A} , {KEY_B} ")


@pytest.fixture
def secured(secured_settings: Settings) -> Iterator[TestClient]:
    app = create_app(secured_settings, converter=FakeConverter(), locator=FakeLocator())  # type: ignore[arg-type]
    with TestClient(app) as client:
        yield client


def _post(client: TestClient, key: str | None = None, data: bytes | None = None):  # type: ignore[no-untyped-def]
    headers = {"X-API-Key": key} if key else {}
    files = {"file": ("report.docx", data if data is not None else make_docx(), "application/octet-stream")}
    return client.post("/jobs", files=files, headers=headers)


def test_keys_are_parsed_and_trimmed(secured_settings: Settings) -> None:
    assert secured_settings.api_key_list == (KEY_A, KEY_B)


def test_empty_keys_disable_authentication(settings: Settings) -> None:
    assert settings.api_key_list == ()
    assert is_authorized((), None)


@pytest.mark.parametrize("value", ["short", f"{KEY_A},tiny"])
def test_short_keys_are_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValidationError, match="16"):
        make_settings(tmp_path, api_keys=value)


def test_health_ready_and_docs_stay_public(secured: TestClient) -> None:
    assert secured.get("/health").status_code == 200
    assert secured.get("/ready").status_code == 200
    assert secured.get("/docs").status_code == 200


@pytest.mark.parametrize("key", [None, "", "wrong-key-00000000000000", KEY_A[:-1], KEY_A + "x"])
def test_missing_or_wrong_key_is_rejected(secured: TestClient, secured_settings: Settings, key: str | None) -> None:
    response = _post(secured, key)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert response.headers["WWW-Authenticate"] == "ApiKey"
    assert list(secured_settings.upload_dir.iterdir()) == []  # no upload stored


@pytest.mark.parametrize("key", [KEY_A, KEY_B])
def test_every_configured_key_works(secured: TestClient, key: str) -> None:
    response = _post(secured, key)
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    headers = {"X-API-Key": key}
    assert secured.get(f"/jobs/{job_id}", headers=headers).status_code == 200


def test_all_job_endpoints_are_protected(secured: TestClient) -> None:
    headers = {"X-API-Key": KEY_A}
    job_id = _post(secured, KEY_A).json()["job_id"]
    for _ in range(200):
        if secured.get(f"/jobs/{job_id}", headers=headers).json()["status"] == "completed":
            break
    for method, path in [
        ("GET", "/jobs"),
        ("GET", "/jobs/"),
        ("GET", f"/jobs/{job_id}"),
        ("GET", f"/jobs/{job_id}/download"),
        ("DELETE", f"/jobs/{job_id}"),
    ]:
        assert secured.request(method, path).status_code == 401, path
    assert secured.get(f"/jobs/{job_id}/download", headers=headers).status_code == 200
    assert secured.delete(f"/jobs/{job_id}", headers=headers).status_code == 204


def test_large_unauthenticated_body_is_not_read(secured: TestClient, secured_settings: Settings) -> None:
    response = _post(secured, None, b"\x00" * (secured_settings.max_upload_bytes * 2))
    assert response.status_code == 401  # 401, not 413: rejected before looking at the body


def test_wait_mode_also_requires_key(secured: TestClient) -> None:
    files = {"file": ("report.docx", make_docx(), "application/octet-stream")}
    assert secured.post("/jobs?wait=5", files=files).status_code == 401
    ok = secured.post("/jobs?wait=5", files=files, headers={"X-API-Key": KEY_B})
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "application/pdf"


def test_openapi_declares_api_key_scheme(secured: TestClient) -> None:
    schema = secured.get("/openapi.json").json()
    scheme = schema["components"]["securitySchemes"]["APIKeyHeader"]
    assert scheme == {**scheme, "type": "apiKey", "in": "header", "name": "X-API-Key"}
    assert "security" in schema["paths"]["/jobs"]["post"]
    assert "security" not in schema["paths"]["/health"]["get"]


def test_without_keys_everything_is_open(client: TestClient) -> None:
    response = client.post("/jobs", files={"file": ("a.docx", make_docx(), "application/octet-stream")})
    assert response.status_code == 202
    assert wait_for_status(client, response.json()["job_id"])["status"] == "completed"
