"""Metadata persistence and job recovery after a restart or a crash."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.jobs.manager import INPUT_MISSING_MESSAGE, INTERRUPTED_MESSAGE
from app.jobs.repository import JobRepository, JobStatus
from app.main import create_app
from tests.conftest import MINIMAL_PDF, FakeConverter, FakeLocator, make_settings, upload, wait_for_status
from tests.ooxml_factory import make_docx


def _start(settings: Settings, converter: FakeConverter | None = None) -> TestClient:
    app = create_app(settings, converter=converter or FakeConverter(), locator=FakeLocator())  # type: ignore[arg-type]
    return TestClient(app)


def _seed(settings: Settings, status: JobStatus, *, attempts: int = 0, with_input: bool = True) -> str:
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    repo = JobRepository(settings.database_path)
    repo.init()
    job_id = str(uuid.uuid4())
    input_name = f"{job_id}.docx"
    if with_input:
        (settings.upload_dir / input_name).write_bytes(make_docx())
    repo.create(
        job_id=job_id, original_filename="resumed.docx", extension=".docx",
        input_name=input_name, input_size=10, now=time.time(),
    )
    if status == JobStatus.PROCESSING:
        for _ in range(max(attempts, 1)):
            repo.requeue(job_id, time.time())
            assert repo.claim(job_id, time.time())
    return job_id


def test_metadata_and_results_survive_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with _start(settings) as client:
        job_id = upload(client, make_docx(), "persistent.docx").json()["job_id"]
        wait_for_status(client, job_id)

    with _start(settings) as client:
        body = client.get(f"/jobs/{job_id}").json()
        assert body["status"] == "completed"
        assert body["original_filename"] == "persistent.docx"
        download = client.get(f"/jobs/{job_id}/download")
        assert download.status_code == 200
        assert download.content == MINIMAL_PDF


def test_interrupted_job_is_requeued(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    job_id = _seed(settings, JobStatus.PROCESSING, attempts=1)
    stale_work = settings.work_dir / job_id
    stale_work.mkdir(parents=True)

    converter = FakeConverter()
    with _start(settings, converter) as client:
        final = wait_for_status(client, job_id)
        assert final["status"] == "completed"
        assert converter.calls == [job_id]
    assert JobRepository(settings.database_path).get(job_id).attempts == 2  # type: ignore[union-attr]


def test_interrupted_job_beyond_max_attempts_fails(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_attempts=2)
    job_id = _seed(settings, JobStatus.PROCESSING, attempts=2)
    converter = FakeConverter()
    with _start(settings, converter) as client:
        body = client.get(f"/jobs/{job_id}").json()
        assert body["status"] == "failed"
        assert body["error"] == INTERRUPTED_MESSAGE
        assert converter.calls == []
    assert not any(settings.upload_dir.iterdir())


def test_interrupted_job_without_input_fails(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    job_id = _seed(settings, JobStatus.PROCESSING, with_input=False)
    with _start(settings) as client:
        assert client.get(f"/jobs/{job_id}").json()["status"] == "failed"


def test_queued_jobs_are_resumed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    resumed = _seed(settings, JobStatus.QUEUED)
    missing = _seed(settings, JobStatus.QUEUED, with_input=False)
    with _start(settings) as client:
        assert wait_for_status(client, resumed)["status"] == "completed"
        body = client.get(f"/jobs/{missing}").json()
        assert body["status"] == "failed"
        assert body["error"] == INPUT_MISSING_MESSAGE


def test_no_job_left_processing_after_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_attempts=1)
    ids = [_seed(settings, JobStatus.PROCESSING) for _ in range(3)]
    with _start(settings):
        pass
    repo = JobRepository(settings.database_path)
    assert all(repo.get(job_id).status == JobStatus.FAILED for job_id in ids)  # type: ignore[union-attr]
