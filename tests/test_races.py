"""Race conditions between conversion, download, deletion and cleanup."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.jobs.manager import JobManager
from app.main import create_app
from tests.conftest import FakeConverter, FakeLocator, make_settings, upload, wait_for_status, wait_until
from tests.ooxml_factory import make_docx


def _manager(app: FastAPI) -> JobManager:
    return app.state.manager


def test_delete_while_processing_is_rejected(client: TestClient, fake_converter: FakeConverter) -> None:
    fake_converter.gate = threading.Event()
    job_id = upload(client, make_docx()).json()["job_id"]
    assert fake_converter.started.wait(5)
    response = client.delete(f"/jobs/{job_id}")
    assert response.status_code == 409
    assert response.json()["code"] == "job_in_progress"
    fake_converter.gate.set()
    assert wait_for_status(client, job_id)["status"] == "completed"
    assert client.delete(f"/jobs/{job_id}").status_code == 204


def test_delete_queued_job_is_never_converted(tmp_path: Path) -> None:
    converter = FakeConverter()
    converter.gate = threading.Event()
    settings = make_settings(tmp_path, max_workers=1)
    app = create_app(settings, converter=converter, locator=FakeLocator())  # type: ignore[arg-type]
    with TestClient(app) as client:
        first = upload(client, make_docx()).json()["job_id"]
        assert converter.started.wait(5)
        second = upload(client, make_docx()).json()["job_id"]
        assert client.get(f"/jobs/{second}").json()["status"] == "queued"

        assert client.delete(f"/jobs/{second}").status_code == 204
        converter.gate.set()
        wait_for_status(client, first)
        third = upload(client, make_docx()).json()["job_id"]
        wait_for_status(client, third)

        assert second not in converter.calls
        assert converter.calls == [first, third]
        assert not any(p.name.startswith(second) for p in settings.upload_dir.iterdir())


def test_delete_during_download_defers_file_removal(client: TestClient, app: FastAPI) -> None:
    job_id = upload(client, make_docx()).json()["job_id"]
    wait_for_status(client, job_id)
    manager = _manager(app)

    lease = manager.open_download(job_id)  # download in progress
    assert client.delete(f"/jobs/{job_id}").status_code == 204
    assert client.get(f"/jobs/{job_id}").status_code == 404
    assert lease.path.exists(), "the PDF must not disappear during a download"
    assert client.get(f"/jobs/{job_id}/download").status_code == 404

    lease.release()
    assert not lease.path.exists()
    lease.release()  # idempotent
    assert manager.active_downloads(job_id) == 0


def test_cleanup_skips_jobs_being_downloaded(client: TestClient, app: FastAPI) -> None:
    job_id = upload(client, make_docx()).json()["job_id"]
    wait_for_status(client, job_id)
    manager = _manager(app)
    far_future = 10**12

    lease = manager.open_download(job_id)
    assert manager.run_cleanup(now=far_future) == 0
    assert client.get(f"/jobs/{job_id}").status_code == 200
    lease.release()
    assert manager.run_cleanup(now=far_future) == 1
    assert client.get(f"/jobs/{job_id}").status_code == 404
    assert not lease.path.exists()


def test_concurrent_downloads_share_the_file(client: TestClient, app: FastAPI) -> None:
    job_id = upload(client, make_docx()).json()["job_id"]
    wait_for_status(client, job_id)
    manager = _manager(app)
    first = manager.open_download(job_id)
    second = manager.open_download(job_id)
    assert manager.active_downloads(job_id) == 2
    client.delete(f"/jobs/{job_id}")
    first.release()
    assert second.path.exists()
    second.release()
    assert not second.path.exists()


def test_download_releases_lease_after_response(client: TestClient, app: FastAPI) -> None:
    job_id = upload(client, make_docx()).json()["job_id"]
    wait_for_status(client, job_id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.get(f"/jobs/{job_id}/download"), range(8)))
    assert all(r.status_code == 200 for r in responses)
    wait_until(lambda: _manager(app).active_downloads(job_id) == 0)


def test_concurrent_deletes_only_one_wins(client: TestClient) -> None:
    job_id = upload(client, make_docx()).json()["job_id"]
    wait_for_status(client, job_id)
    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = sorted(pool.map(lambda _: client.delete(f"/jobs/{job_id}").status_code, range(6)))
    assert codes.count(204) == 1
    assert set(codes) == {204, 404}


def test_concurrency_limit_is_respected(tmp_path: Path) -> None:
    converter = FakeConverter()
    converter.gate = threading.Event()
    settings = make_settings(tmp_path, max_workers=2)
    app = create_app(settings, converter=converter, locator=FakeLocator())  # type: ignore[arg-type]
    with TestClient(app) as client:
        ids = [upload(client, make_docx()).json()["job_id"] for _ in range(5)]
        wait_until(lambda: len(converter.calls) == 2)
        statuses = [client.get(f"/jobs/{job_id}").json()["status"] for job_id in ids]
        assert statuses.count("processing") == 2
        assert statuses.count("queued") == 3
        converter.gate.set()
        for job_id in ids:
            assert wait_for_status(client, job_id)["status"] == "completed"


def test_queue_full_is_rejected(tmp_path: Path) -> None:
    converter = FakeConverter()
    converter.gate = threading.Event()
    settings = make_settings(tmp_path, max_workers=1, max_queued_jobs=1)
    app = create_app(settings, converter=converter, locator=FakeLocator())  # type: ignore[arg-type]
    with TestClient(app) as client:
        upload(client, make_docx())
        assert converter.started.wait(5)
        assert upload(client, make_docx()).status_code == 202
        rejected = upload(client, make_docx())
        assert rejected.status_code == 503
        assert rejected.json()["code"] == "queue_full"
        converter.gate.set()
