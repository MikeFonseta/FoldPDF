"""Cleanup of expired jobs and orphaned files."""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.jobs.manager import JobManager
from tests.conftest import FakeConverter, upload, wait_for_status
from tests.ooxml_factory import make_docx


def _manager(app: FastAPI) -> JobManager:
    return app.state.manager


def _age(path: Path, seconds: float) -> None:
    past = time.time() - seconds
    os.utime(path, (past, past))


def test_expired_jobs_are_removed(client: TestClient, app: FastAPI, settings: Settings) -> None:
    job_id = upload(client, make_docx()).json()["job_id"]
    final = wait_for_status(client, job_id)
    result = settings.result_dir / f"{job_id}.pdf"
    assert result.exists()

    manager = _manager(app)
    assert manager.run_cleanup(now=time.time()) == 0  # not expired yet
    assert result.exists()

    expires_at = time.time() + settings.job_ttl_seconds + 1
    assert manager.run_cleanup(now=expires_at) == 1
    assert not result.exists()
    assert client.get(f"/jobs/{job_id}").status_code == 404
    assert final["expires_at"] is not None


def test_failed_jobs_expire_too(client: TestClient, app: FastAPI, fake_converter: FakeConverter) -> None:
    fake_converter.mode = "fail"
    job_id = upload(client, make_docx()).json()["job_id"]
    wait_for_status(client, job_id)
    assert _manager(app).run_cleanup(now=time.time() + 10**6) == 1
    assert client.get(f"/jobs/{job_id}").status_code == 404


def test_active_jobs_are_never_removed(
    client: TestClient, app: FastAPI, fake_converter: FakeConverter, settings: Settings
) -> None:
    fake_converter.gate = threading.Event()
    job_id = upload(client, make_docx()).json()["job_id"]
    assert fake_converter.started.wait(5)
    manager = _manager(app)
    work = settings.work_dir / job_id
    work.mkdir()
    _age(work, 10**5)

    assert manager.run_cleanup(now=time.time() + 10**9) == 0
    assert client.get(f"/jobs/{job_id}").json()["status"] == "processing"
    assert work.exists(), "the working directory of an active job must not be removed"
    assert any(p.name.startswith(job_id) for p in settings.upload_dir.iterdir())
    fake_converter.gate.set()
    assert wait_for_status(client, job_id)["status"] == "completed"


def test_orphan_files_are_swept(client: TestClient, app: FastAPI, settings: Settings) -> None:
    orphan_id = str(uuid.uuid4())
    orphan_result = settings.result_dir / f"{orphan_id}.pdf"
    orphan_upload = settings.upload_dir / f"{orphan_id}.docx"
    stale_part = settings.upload_dir / f"{uuid.uuid4()}.part"
    fresh_part = settings.upload_dir / f"{uuid.uuid4()}.part"
    orphan_work = settings.work_dir / orphan_id
    stale_probe = settings.profile_dir / "probe-abc123"
    foreign = settings.result_dir / "user-file.txt"
    for path in (orphan_result, orphan_upload, stale_part, fresh_part, foreign):
        path.write_bytes(b"x")
    orphan_work.mkdir()
    stale_probe.mkdir()
    for path in (orphan_result, orphan_upload, stale_part, orphan_work, stale_probe, foreign):
        _age(path, 2 * 3600)

    recent_orphan = settings.result_dir / f"{uuid.uuid4()}.pdf"
    recent_orphan.write_bytes(b"x")

    _manager(app).run_cleanup()

    for path in (orphan_result, orphan_upload, stale_part, orphan_work, stale_probe):
        assert not path.exists(), path
    assert fresh_part.exists(), "upload in progress"
    assert recent_orphan.exists(), "grace period"
    assert foreign.exists(), "files not managed by the service must never be removed"
