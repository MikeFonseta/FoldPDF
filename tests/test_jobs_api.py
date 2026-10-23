from __future__ import annotations

import threading
import uuid
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from tests.conftest import MINIMAL_PDF, FakeConverter, upload, wait_for_status
from tests.ooxml_factory import SAMPLES, make_docx


@pytest.mark.parametrize("extension", [".docx", ".xlsx", ".pptx"])
def test_full_job_lifecycle(client: TestClient, extension: str) -> None:
    response = upload(client, SAMPLES[extension](), f"report{extension}")
    assert response.status_code == 202
    body = response.json()
    job_id = body["job_id"]
    assert uuid.UUID(job_id)
    assert body["status"] in {"queued", "processing", "completed"}
    assert body["original_filename"] == f"report{extension}"
    assert body["status_url"] == f"/jobs/{job_id}"
    assert response.headers["Location"] == f"/jobs/{job_id}"

    final = wait_for_status(client, job_id)
    assert final["status"] == "completed"
    assert final["download_url"] == f"/jobs/{job_id}/download"
    assert final["error"] is None
    assert final["expires_at"] is not None

    download = client.get(final["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"
    assert download.headers["content-disposition"].startswith('attachment; filename="report.pdf"')
    assert download.headers["cache-control"] == "no-store"
    assert download.content == MINIMAL_PDF


def test_status_transitions(client: TestClient, fake_converter: FakeConverter) -> None:
    fake_converter.gate = threading.Event()
    job_id = upload(client, make_docx()).json()["job_id"]
    assert fake_converter.started.wait(5)
    assert client.get(f"/jobs/{job_id}").json()["status"] == "processing"
    fake_converter.gate.set()
    assert wait_for_status(client, job_id)["status"] == "completed"


@pytest.mark.parametrize("filename", ["report.doc", "photo.png", "no_extension", "macro.docm"])
def test_unsupported_extension(client: TestClient, filename: str) -> None:
    response = upload(client, make_docx(), filename)
    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_file_type"


def test_empty_file(client: TestClient) -> None:
    response = upload(client, b"", "empty.docx")
    assert response.status_code == 400
    assert response.json()["code"] == "empty_file"


def test_corrupted_file(client: TestClient, settings: Settings) -> None:
    response = upload(client, b"PK\x03\x04 not really a zip", "broken.docx")
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_document"
    # no leftover file in the upload directory
    assert list(settings.upload_dir.iterdir()) == []


def test_mismatched_content(client: TestClient) -> None:
    response = upload(client, SAMPLES[".pptx"](), "fake.docx")
    assert response.status_code == 400
    assert "DOCX" in response.json()["detail"]


def test_file_too_large_by_content(client: TestClient, settings: Settings) -> None:
    # The size exceeds MAX_UPLOAD_BYTES but stays within the multipart allowance:
    # the limit is enforced while reading in chunks.
    data = make_docx() + b"\x00" * (settings.max_upload_bytes + 1)
    response = upload(client, data[: settings.max_upload_bytes + 10], "large.docx")
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"
    assert list(settings.upload_dir.iterdir()) == []


def test_file_too_large_by_content_length(client: TestClient, settings: Settings) -> None:
    data = b"\x00" * (settings.max_upload_bytes * 2)
    response = upload(client, data, "huge.docx")
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"


def test_missing_file_field(client: TestClient) -> None:
    response = client.post("/jobs", data={"other": "value"})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize("job_id", [str(uuid.uuid4()), "not-a-uuid", "..%2F..%2Fetc"])
def test_nonexistent_job(client: TestClient, job_id: str) -> None:
    assert client.get(f"/jobs/{job_id}").status_code == 404
    assert client.get(f"/jobs/{job_id}/download").status_code == 404
    assert client.delete(f"/jobs/{job_id}").status_code == 404


def test_premature_download(client: TestClient, fake_converter: FakeConverter) -> None:
    fake_converter.gate = threading.Event()
    job_id = upload(client, make_docx()).json()["job_id"]
    response = client.get(f"/jobs/{job_id}/download")
    assert response.status_code == 409
    assert response.json()["code"] == "job_not_ready"
    fake_converter.gate.set()
    wait_for_status(client, job_id)
    assert client.get(f"/jobs/{job_id}/download").status_code == 200


def test_conversion_failure(client: TestClient, fake_converter: FakeConverter, settings: Settings) -> None:
    fake_converter.mode = "fail"
    job_id = upload(client, make_docx()).json()["job_id"]
    final = wait_for_status(client, job_id)
    assert final["status"] == "failed"
    assert final["error"] == "LibreOffice could not convert the document."
    assert final["download_url"] is None
    download = client.get(f"/jobs/{job_id}/download")
    assert download.status_code == 409
    assert "failed" in download.json()["detail"]
    assert list(settings.upload_dir.iterdir()) == []  # input removed once the job finished


def test_conversion_timeout(client: TestClient, fake_converter: FakeConverter) -> None:
    fake_converter.mode = "timeout"
    job_id = upload(client, make_docx()).json()["job_id"]
    final = wait_for_status(client, job_id)
    assert final["status"] == "failed"
    assert "maximum time" in final["error"]


def test_internal_error_is_not_exposed(client: TestClient, fake_converter: FakeConverter) -> None:
    fake_converter.mode = "crash"
    job_id = upload(client, make_docx()).json()["job_id"]
    final = wait_for_status(client, job_id)
    assert final["status"] == "failed"
    assert final["error"] == "Internal error during the conversion."
    assert "secret" not in str(final)


def test_unsafe_filenames(client: TestClient, settings: Settings) -> None:
    response = upload(client, make_docx(), '../../evil"; name=x\r\n.docx')
    assert response.status_code == 202
    body = response.json()
    assert "/" not in body["original_filename"] and '"' not in body["original_filename"]
    final = wait_for_status(client, body["job_id"])
    header = client.get(final["download_url"]).headers["content-disposition"]
    assert "\r" not in header and "\n" not in header and ".." not in header
    # files on disk have internal names independent of the client
    assert all(path.name.startswith(body["job_id"]) for path in settings.result_dir.iterdir())


def test_unicode_filename_download(client: TestClient) -> None:
    job_id = upload(client, make_docx(), "résumé final.docx").json()["job_id"]
    final = wait_for_status(client, job_id)
    header = client.get(final["download_url"]).headers["content-disposition"]
    assert 'filename="resume final.pdf"' in header
    assert unquote(header.split("filename*=UTF-8''")[1]) == "résumé final.pdf"


def test_list_jobs_with_pagination(client: TestClient) -> None:
    ids = [upload(client, make_docx(), f"doc{i}.docx").json()["job_id"] for i in range(3)]
    for job_id in ids:
        wait_for_status(client, job_id)
    page = client.get("/jobs", params={"limit": 2, "offset": 0}).json()
    assert page["total"] == 3
    assert len(page["items"]) == 2
    rest = client.get("/jobs", params={"limit": 2, "offset": 2}).json()
    assert len(rest["items"]) == 1
    assert {item["job_id"] for item in page["items"] + rest["items"]} == set(ids)
    assert client.get("/jobs", params={"limit": 0}).status_code == 422
    assert client.get("/jobs", params={"limit": 101}).status_code == 422


def test_delete_completed_job(client: TestClient, settings: Settings) -> None:
    job_id = upload(client, make_docx()).json()["job_id"]
    wait_for_status(client, job_id)
    assert (settings.result_dir / f"{job_id}.pdf").exists()
    assert client.delete(f"/jobs/{job_id}").status_code == 204
    assert client.get(f"/jobs/{job_id}").status_code == 404
    assert not (settings.result_dir / f"{job_id}.pdf").exists()
    assert client.delete(f"/jobs/{job_id}").status_code == 404
