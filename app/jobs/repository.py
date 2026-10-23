"""Persistence of job metadata in SQLite.

Every operation opens its own connection (thread-safe) and writes are
serialized by a lock. State transitions use conditional UPDATEs
(`WHERE status = ...`) so two concurrent operations can never both "win" on
the same job.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.services.storage import make_private

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    extension         TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('queued', 'processing', 'completed', 'failed')),
    input_name        TEXT NOT NULL,
    input_size        INTEGER NOT NULL,
    result_name       TEXT,
    result_size       INTEGER,
    error_message     TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    started_at        REAL,
    finished_at       REAL,
    expires_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_expires_at ON jobs (expires_at);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs (created_at);
"""


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_STATUSES = (JobStatus.COMPLETED, JobStatus.FAILED)


@dataclass(frozen=True)
class JobRecord:
    id: str
    original_filename: str
    extension: str
    status: JobStatus
    input_name: str
    input_size: int
    result_name: str | None
    result_size: int | None
    error_message: str | None
    attempts: int
    created_at: float
    updated_at: float
    started_at: float | None
    finished_at: float | None
    expires_at: float | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> JobRecord:
        data = dict(row)
        data["status"] = JobStatus(data["status"])
        return cls(**data)


def _placeholders(values: Iterable[object]) -> str:
    return ", ".join("?" for _ in values)


class JobRepository:
    def __init__(self, database_path: Path) -> None:
        self._path = database_path
        self._write_lock = threading.Lock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock, self._connect() as conn:
            yield conn

    def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._write() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        make_private(self._path)

    def ping(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def create(
        self, *, job_id: str, original_filename: str, extension: str, input_name: str, input_size: int, now: float
    ) -> JobRecord:
        with self._write() as conn:
            conn.execute(
                """INSERT INTO jobs (id, original_filename, extension, status, input_name, input_size,
                                     created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, original_filename, extension, JobStatus.QUEUED, input_name, input_size, now, now),
            )
        record = self.get(job_id)
        assert record is not None
        return record

    def get(self, job_id: str) -> JobRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return JobRecord.from_row(row) if row else None

    def list_page(self, limit: int, offset: int) -> tuple[list[JobRecord], int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return [JobRecord.from_row(row) for row in rows], total

    def list_by_status(self, *statuses: JobStatus) -> list[JobRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({_placeholders(statuses)}) ORDER BY created_at",
                [str(s) for s in statuses],
            ).fetchall()
        return [JobRecord.from_row(row) for row in rows]

    def list_expired(self, now: float) -> list[JobRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM jobs
                    WHERE status IN ({_placeholders(TERMINAL_STATUSES)}) AND expires_at IS NOT NULL
                      AND expires_at <= ?
                    ORDER BY expires_at""",
                [*(str(s) for s in TERMINAL_STATUSES), now],
            ).fetchall()
        return [JobRecord.from_row(row) for row in rows]

    def all_ids(self) -> set[str]:
        with self._connect() as conn:
            return {row[0] for row in conn.execute("SELECT id FROM jobs")}

    def claim(self, job_id: str, now: float) -> bool:
        """queued -> processing. Returns False if the job is no longer queued (e.g. deleted)."""
        with self._write() as conn:
            cursor = conn.execute(
                """UPDATE jobs SET status = ?, attempts = attempts + 1, started_at = ?, updated_at = ?
                   WHERE id = ? AND status = ?""",
                (JobStatus.PROCESSING, now, now, job_id, JobStatus.QUEUED),
            )
            return cursor.rowcount == 1

    def complete(self, job_id: str, *, result_name: str, result_size: int, now: float, expires_at: float) -> bool:
        with self._write() as conn:
            cursor = conn.execute(
                """UPDATE jobs SET status = ?, result_name = ?, result_size = ?, error_message = NULL,
                                   finished_at = ?, updated_at = ?, expires_at = ?
                   WHERE id = ? AND status = ?""",
                (JobStatus.COMPLETED, result_name, result_size, now, now, expires_at, job_id, JobStatus.PROCESSING),
            )
            return cursor.rowcount == 1

    def fail(
        self, job_id: str, *, message: str, now: float, expires_at: float, from_statuses: Iterable[JobStatus]
    ) -> bool:
        statuses = [str(s) for s in from_statuses]
        with self._write() as conn:
            cursor = conn.execute(
                f"""UPDATE jobs SET status = ?, error_message = ?, finished_at = ?, updated_at = ?, expires_at = ?
                    WHERE id = ? AND status IN ({_placeholders(statuses)})""",
                (JobStatus.FAILED, message, now, now, expires_at, job_id, *statuses),
            )
            return cursor.rowcount == 1

    def requeue(self, job_id: str, now: float) -> bool:
        """processing -> queued, used by the recovery after a restart."""
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = ?, started_at = NULL, updated_at = ? WHERE id = ? AND status = ?",
                (JobStatus.QUEUED, now, job_id, JobStatus.PROCESSING),
            )
            return cursor.rowcount == 1

    def delete(self, job_id: str, allowed_statuses: Iterable[JobStatus]) -> bool:
        statuses = [str(s) for s in allowed_statuses]
        with self._write() as conn:
            cursor = conn.execute(
                f"DELETE FROM jobs WHERE id = ? AND status IN ({_placeholders(statuses)})",
                (job_id, *statuses),
            )
            return cursor.rowcount == 1
