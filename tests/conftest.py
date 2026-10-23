"""Shared fixtures. The LibreOffice process is replaced by a controllable fake converter."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.converter import ConversionFailedError, ConversionResult, ConversionTimeoutError
from app.services.libreoffice import EngineStatus

MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Kids [] /Count 0 >> endobj\n"
    b"trailer << /Root 1 0 R >>\n%%EOF\n"
)

_SETTING_ENV_VARS = {name.upper() for name in Settings.model_fields}


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host environment variables from affecting the tests."""
    for name in list(os.environ):
        if name.upper() in _SETTING_ENV_VARS:
            monkeypatch.delenv(name, raising=False)


class FakeLocator:
    def __init__(self, available: bool = True) -> None:
        self.available = available

    @property
    def status(self) -> EngineStatus:
        if self.available:
            return EngineStatus(True, Path("soffice"), "24.8.0", "LibreOffice available.", time.time())
        return EngineStatus(False, None, None, "LibreOffice was not found.", time.time())

    def detect(self) -> EngineStatus:
        return self.status

    def refresh_if_stale(self, max_age: float = 30.0) -> EngineStatus:
        return self.status


class FakeConverter:
    """Simulates LibreOffice: `mode` selects the outcome, `gate` can block the conversion."""

    def __init__(self) -> None:
        self.mode = "success"
        self.gate: threading.Event | None = None
        self.started = threading.Event()
        self.calls: list[str] = []

    def convert(self, job_id: str, input_path: Path, output_path: Path) -> ConversionResult:
        self.calls.append(job_id)
        self.started.set()
        assert input_path.is_file()
        if self.gate is not None:
            assert self.gate.wait(15), "gate was never released"
        if self.mode == "fail":
            raise ConversionFailedError("LibreOffice could not convert the document.")
        if self.mode == "timeout":
            raise ConversionTimeoutError("The conversion exceeded the maximum time of 1 seconds.")
        if self.mode == "crash":
            raise RuntimeError("internal failure in /secret/path/soffice")
        output_path.write_bytes(MINIMAL_PDF)
        return ConversionResult(size=len(MINIMAL_PDF), duration=0.01)


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "max_workers": 2,
        "conversion_timeout_seconds": 5,
        "job_ttl_seconds": 3600,
        "cleanup_interval_seconds": 3600,
        "max_upload_bytes": 1024 * 1024,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def fake_converter() -> FakeConverter:
    return FakeConverter()


@pytest.fixture
def fake_locator() -> FakeLocator:
    return FakeLocator()


@pytest.fixture
def app(settings: Settings, fake_converter: FakeConverter, fake_locator: FakeLocator) -> FastAPI:
    return create_app(settings, converter=fake_converter, locator=fake_locator)  # type: ignore[arg-type]


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def wait_for_status(
    client: TestClient, job_id: str, statuses: tuple[str, ...] = ("completed", "failed"), timeout: float = 10
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/jobs/{job_id}").json()
        if body.get("status") in statuses:
            return body
        time.sleep(0.02)
    raise AssertionError(f"Job {job_id} did not reach {statuses}: {body}")


def wait_until(predicate: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Condition not met before the timeout")


def upload(client: TestClient, content: bytes, filename: str = "report.docx"):  # type: ignore[no-untyped-def]
    return client.post("/jobs", files={"file": (filename, content, "application/octet-stream")})
