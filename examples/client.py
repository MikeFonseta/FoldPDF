"""Example client (standard library only): uploads a document and saves the PDF.

It first tries the single-call mode (`POST /jobs?wait=N`); if the conversion
takes longer, it falls back to polling the job status.

Usage:
    python examples/client.py report.docx [--url http://127.0.0.1:8080] [--out report.pdf]
                                          [--api-key KEY] [--wait 30]

The API key can also be provided through the FOLDPDF_API_KEY environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


class Client:
    def __init__(self, base_url: str, api_key: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key} if api_key else {}

    def request(self, method: str, path: str, data: bytes | None = None, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, method=method, headers={**self.headers, **(headers or {})}
        )
        try:
            return urllib.request.urlopen(request, timeout=300)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise SystemExit(f"HTTP error {exc.code}: {body}") from None

    def submit(self, path: Path, wait: int):  # type: ignore[no-untyped-def]
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        return self.request("POST", f"/jobs?wait={wait}", body, headers)

    def poll(self, status_url: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.request("GET", status_url) as response:
                job = json.load(response)
            if job["status"] in ("completed", "failed"):
                return job
            time.sleep(1)
        raise SystemExit("Timed out waiting for the conversion")


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert an Office document to PDF with FoldPDF")
    parser.add_argument("file", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--api-key", default=os.environ.get("FOLDPDF_API_KEY"))
    parser.add_argument("--wait", type=int, default=30, help="seconds to wait in the first call (0 = polling only)")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()

    client = Client(args.url, args.api_key)
    output = args.out or args.file.with_suffix(".pdf")

    with client.submit(args.file, args.wait) as response:
        if response.status == 200:  # PDF ready in the same call
            output.write_bytes(response.read())
            print(f"PDF saved to {output} (job {response.headers['X-Job-Id']}, single call)")
            return 0
        job = json.load(response)

    print(f"Job {job['job_id']} still running ({job['status']}): polling...")
    job = client.poll(job["status_url"], args.timeout)
    if job["status"] == "failed":
        print(f"Conversion failed: {job['error']}", file=sys.stderr)
        return 1
    with client.request("GET", job["download_url"]) as response:
        output.write_bytes(response.read())
    print(f"PDF saved to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
