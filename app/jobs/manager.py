"""Job management: in-process queue, workers, restart recovery, downloads and cleanup.

Execution model: a single service instance (one uvicorn process) owns an
`asyncio` queue and `MAX_WORKERS` workers; every (blocking) conversion runs in a
thread through `asyncio.to_thread`. SQLite is the source of truth: the in-memory
queue is rebuilt at startup from jobs still `queued`, and jobs left `processing`
after a crash are re-queued or marked `failed`.

Concurrency:
- state transitions are conditional UPDATEs in the repository;
- `self._lock` makes "read status + download lease" and "check lease + delete"
  atomic, so a PDF is never removed while it is being downloaded (removal is
  postponed until the lease is released).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.config import Settings
from app.exceptions import (
    EngineUnavailableError,
    JobInProgressError,
    JobNotFoundError,
    JobNotReadyError,
    QueueFullError,
    ResultUnavailableError,
    ServiceUnavailableError,
)
from app.jobs.repository import TERMINAL_STATUSES, JobRecord, JobRepository, JobStatus
from app.services.converter import ConversionError, ConversionResult
from app.services.libreoffice import LibreOfficeLocator
from app.services.storage import MANAGED_FILE_RE, Storage, is_uuid, remove_file, remove_tree
from app.services.validation import pdf_filename

logger = logging.getLogger(__name__)

INTERRUPTED_MESSAGE = "The conversion was interrupted by a service restart."
INPUT_MISSING_MESSAGE = "The uploaded file is no longer available."
INTERNAL_ERROR_MESSAGE = "Internal error during the conversion."


class Converter(Protocol):
    def convert(self, job_id: str, input_path: Path, output_path: Path) -> ConversionResult: ...


@dataclass(frozen=True)
class DownloadLease:
    path: Path
    filename: str
    release: Callable[[], None]


@dataclass(frozen=True)
class Readiness:
    ready: bool
    checks: dict[str, bool]
    libreoffice_version: str | None
    detail: str


class JobManager:
    orphan_grace_seconds: float = 300
    part_grace_seconds: float = 3600

    def __init__(
        self,
        settings: Settings,
        repository: JobRepository,
        storage: Storage,
        converter: Converter,
        locator: LibreOfficeLocator,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._repo = repository
        self._storage = storage
        self._converter = converter
        self._locator = locator
        self._clock = clock
        self._lock = threading.Lock()
        self._leases: dict[str, int] = {}
        self._pending_removal: dict[str, list[Path]] = {}
        self._queue: asyncio.Queue[str] | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._busy: set[asyncio.Task[None]] = set()
        self._cleanup_task: asyncio.Task[None] | None = None
        # Events for POST /jobs?wait=N requests waiting for the outcome (used only on the event loop thread).
        self._completion: dict[str, asyncio.Event] = {}
        self._accepting = False
        self._stopping = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._queue = asyncio.Queue()
        self._stopping = False
        for job_id in await asyncio.to_thread(self.recover):
            self._queue.put_nowait(job_id)
        await asyncio.to_thread(self.run_cleanup)
        self._workers = [
            asyncio.create_task(self._worker(), name=f"converter-worker-{i}")
            for i in range(self._settings.max_workers)
        ]
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="job-cleanup")
        self._accepting = True
        logger.info("Job manager started with %d workers", self._settings.max_workers)

    async def stop(self) -> None:
        self._accepting = False
        self._stopping = True
        for event in self._completion.values():
            event.set()  # waiting requests answer immediately with the current status
        if self._cleanup_task:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
        idle = [task for task in self._workers if task not in self._busy]
        busy = [task for task in self._workers if task in self._busy]
        for task in idle:
            task.cancel()
        if busy:
            logger.info("Waiting for %d running conversions to finish", len(busy))
            _, pending = await asyncio.wait(busy, timeout=self._settings.conversion_timeout_seconds + 15)
            for task in pending:
                task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        logger.info("Job manager stopped")

    def recover(self) -> list[str]:
        """Bring jobs left half-done by a previous shutdown back to a consistent state."""
        now = self._clock()
        expires = now + self._settings.job_ttl_seconds
        for job in self._repo.list_by_status(JobStatus.PROCESSING):
            input_path = self._storage.input_path(job.input_name)
            remove_tree(self._settings.work_dir / job.id)
            remove_tree(self._settings.profile_dir / job.id)
            remove_file(self._storage.result_dir / f"{job.id}.pdf.tmp")
            if input_path.is_file() and job.attempts < self._settings.max_attempts:
                self._repo.requeue(job.id, now)
                logger.warning("Job %s was interrupted while processing: re-queued", job.id)
            else:
                self._repo.fail(
                    job.id, message=INTERRUPTED_MESSAGE, now=now, expires_at=expires,
                    from_statuses=[JobStatus.PROCESSING],
                )
                remove_file(input_path)
                logger.warning("Job %s was interrupted while processing: marked as failed", job.id)

        queued: list[str] = []
        for job in self._repo.list_by_status(JobStatus.QUEUED):
            if self._storage.input_path(job.input_name).is_file():
                queued.append(job.id)
            else:
                self._repo.fail(
                    job.id, message=INPUT_MISSING_MESSAGE, now=now, expires_at=expires,
                    from_statuses=[JobStatus.QUEUED],
                )
        if queued:
            logger.info("Resumed %d queued jobs", len(queued))
        return queued

    # ------------------------------------------------------------------ creation

    def ensure_accepting(self) -> None:
        if not self._accepting or self._queue is None:
            raise ServiceUnavailableError()
        if not self._locator.refresh_if_stale().available:
            raise EngineUnavailableError(headers={"Retry-After": "30"})
        if self._queue.qsize() >= self._settings.max_queued_jobs:
            raise QueueFullError(headers={"Retry-After": "30"})

    def submit(
        self, upload_path: Path, original_filename: str, extension: str, size: int, *, notify: bool = False
    ) -> JobRecord:
        """Register and enqueue the job. With `notify=True` its outcome can be awaited with `wait_for_job`."""
        self.ensure_accepting()
        assert self._queue is not None
        job_id = str(uuid.uuid4())
        input_name = f"{job_id}{extension}"
        final_path = self._storage.input_path(input_name)
        os.replace(upload_path, final_path)
        try:
            job = self._repo.create(
                job_id=job_id, original_filename=original_filename, extension=extension,
                input_name=input_name, input_size=size, now=self._clock(),
            )
        except BaseException:
            remove_file(final_path)
            raise
        if notify:
            # Registered before enqueueing: the worker cannot finish before the event exists.
            self._completion[job_id] = asyncio.Event()
        self._queue.put_nowait(job_id)
        logger.info("Job %s created (%s, %d bytes)", job_id, extension, size)
        return job

    async def wait_for_job(self, job_id: str, timeout: float) -> JobRecord:
        """Wait at most `timeout` seconds for the job to finish and return its current state."""
        event = self._completion.get(job_id)
        try:
            if event is not None:
                try:
                    await asyncio.wait_for(event.wait(), timeout)
                except TimeoutError:
                    pass
        finally:
            self._completion.pop(job_id, None)
        return self.get_job(job_id)

    def _notify_finished(self, job_id: str) -> None:
        event = self._completion.get(job_id)
        if event is not None:
            event.set()

    # ------------------------------------------------------------------ queries

    def get_job(self, job_id: str) -> JobRecord:
        job = self._repo.get(job_id) if is_uuid(job_id) else None
        if job is None:
            raise JobNotFoundError()
        return job

    def list_jobs(self, limit: int, offset: int) -> tuple[list[JobRecord], int]:
        return self._repo.list_page(limit, offset)

    def readiness(self) -> Readiness:
        engine = self._locator.refresh_if_stale()
        checks = {
            "libreoffice": engine.available,
            "storage": self._storage.check_writable(),
            "database": self._repo.ping(),
            "workers": self._accepting,
        }
        ready = all(checks.values())
        if ready:
            detail = "Service ready to process conversions."
        elif not engine.available:
            detail = engine.message
        else:
            failed = ", ".join(name for name, ok in checks.items() if not ok)
            detail = f"Failed checks: {failed}."
        return Readiness(ready, checks, engine.version, detail)

    # ------------------------------------------------------------------ download and deletion

    def open_download(self, job_id: str) -> DownloadLease:
        if not is_uuid(job_id):
            raise JobNotFoundError()
        with self._lock:
            job = self._repo.get(job_id)
            if job is None:
                raise JobNotFoundError()
            if job.status == JobStatus.FAILED:
                raise JobNotReadyError("The conversion failed: no PDF is available.")
            if job.status != JobStatus.COMPLETED or not job.result_name:
                raise JobNotReadyError(f"The job is '{job.status}': the PDF is not available yet.")
            path = self._storage.result_path(job.result_name)
            if not path.is_file():
                raise ResultUnavailableError()
            self._leases[job_id] = self._leases.get(job_id, 0) + 1

        released = threading.Event()

        def release() -> None:
            if not released.is_set():
                released.set()
                self._release_lease(job_id)

        return DownloadLease(path=path, filename=pdf_filename(job.original_filename), release=release)

    def active_downloads(self, job_id: str) -> int:
        with self._lock:
            return self._leases.get(job_id, 0)

    def _release_lease(self, job_id: str) -> None:
        with self._lock:
            remaining = self._leases.get(job_id, 0) - 1
            if remaining > 0:
                self._leases[job_id] = remaining
                return
            self._leases.pop(job_id, None)
            paths = self._pending_removal.pop(job_id, [])
        for path in paths:
            remove_file(path)

    def _job_files(self, job: JobRecord) -> list[Path]:
        paths = [self._storage.input_path(job.input_name), self._storage.result_dir / f"{job.id}.pdf.tmp"]
        if job.result_name:
            paths.append(self._storage.result_path(job.result_name))
        return paths

    def _detach_files(self, job: JobRecord) -> list[Path]:
        """Call with `self._lock` held: postpones removal while downloads are active."""
        paths = self._job_files(job)
        if self._leases.get(job.id):
            self._pending_removal[job.id] = paths
            return []
        return paths

    def delete_job(self, job_id: str) -> None:
        if not is_uuid(job_id):
            raise JobNotFoundError()
        with self._lock:
            job = self._repo.get(job_id)
            if job is None:
                raise JobNotFoundError()
            if job.status == JobStatus.PROCESSING:
                raise JobInProgressError()
            if not self._repo.delete(job_id, [JobStatus.QUEUED, *TERMINAL_STATUSES]):
                # The status changed in the meantime (e.g. the job has just started processing).
                if self._repo.get(job_id) is None:
                    raise JobNotFoundError()
                raise JobInProgressError()
            paths = self._detach_files(job)
        for path in paths:
            remove_file(path)
        self._notify_finished(job_id)
        logger.info("Job %s deleted", job_id)

    # ------------------------------------------------------------------ processing

    async def _worker(self) -> None:
        assert self._queue is not None
        task = asyncio.current_task()
        assert task is not None
        while not self._stopping:
            job_id = await self._queue.get()
            if self._stopping:
                break  # the job stays "queued" in the database and is resumed at the next start
            self._busy.add(task)
            try:
                await self._process(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected error while processing job %s", job_id)
            finally:
                self._busy.discard(task)
                self._queue.task_done()
                self._notify_finished(job_id)

    async def _process(self, job_id: str) -> None:
        if not self._repo.claim(job_id, self._clock()):
            return  # deleted or already handled
        job = self._repo.get(job_id)
        if job is None:
            return
        input_path = self._storage.input_path(job.input_name)
        result_name = f"{job.id}.pdf"
        output_path = self._storage.result_path(result_name)
        logger.info("Job %s: conversion started (attempt %d)", job.id, job.attempts)
        try:
            result = await asyncio.to_thread(self._converter.convert, job.id, input_path, output_path)
        except asyncio.CancelledError:
            # Service shutdown: the input is kept and startup recovery will handle the job.
            raise
        except ConversionError as exc:
            logger.warning("Job %s: conversion failed: %s", job.id, exc.public_message)
            self._mark_failed(job.id, exc.public_message)
        except Exception:
            logger.exception("Job %s: internal error during the conversion", job.id)
            self._mark_failed(job.id, INTERNAL_ERROR_MESSAGE)
        else:
            now = self._clock()
            if self._repo.complete(
                job.id, result_name=result_name, result_size=result.size, now=now,
                expires_at=now + self._settings.job_ttl_seconds,
            ):
                logger.info("Job %s: completed in %.1f s (%d bytes)", job.id, result.duration, result.size)
            else:
                remove_file(output_path)
        remove_file(input_path)

    def _mark_failed(self, job_id: str, message: str) -> None:
        now = self._clock()
        self._repo.fail(
            job_id, message=message, now=now, expires_at=now + self._settings.job_ttl_seconds,
            from_statuses=[JobStatus.PROCESSING],
        )

    # ------------------------------------------------------------------ cleanup

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.cleanup_interval_seconds)
            try:
                await asyncio.to_thread(self.run_cleanup)
            except Exception:
                logger.exception("Error during the periodic cleanup")

    def run_cleanup(self, now: float | None = None) -> int:
        """Delete expired jobs and orphaned files. Returns the number of deleted jobs."""
        now = self._clock() if now is None else now
        removed = 0
        for job in self._repo.list_expired(now):
            with self._lock:
                if self._leases.get(job.id):
                    continue  # download in progress: retried at the next run
                if not self._repo.delete(job.id, TERMINAL_STATUSES):
                    continue
                paths = self._detach_files(job)
            for path in paths:
                remove_file(path)
            removed += 1
        if removed:
            logger.info("Cleanup: deleted %d expired jobs", removed)
        self._sweep_orphans(now)
        return removed

    def _sweep_orphans(self, now: float) -> None:
        """Remove service-managed files and directories that do not belong to any active job.

        Only names generated by the service (UUIDs) are considered, never other files.
        """
        known = self._repo.all_ids()
        active = {job.id for job in self._repo.list_by_status(JobStatus.PROCESSING)}
        with self._lock:
            protected = set(self._leases) | set(self._pending_removal)

        def age(path: Path) -> float:
            try:
                return now - path.stat().st_mtime
            except OSError:
                return 0.0

        for directory in (self._storage.upload_dir, self._storage.result_dir):
            for entry in _iterdir(directory):
                match = MANAGED_FILE_RE.match(entry.name)
                if not match or not entry.is_file():
                    continue
                job_id, suffix = match.groups()
                if suffix == ".part":
                    stale = age(entry) > self.part_grace_seconds
                elif suffix == ".pdf.tmp":
                    stale = job_id not in active and age(entry) > self.orphan_grace_seconds
                else:
                    stale = job_id not in known and job_id not in protected and age(entry) > self.orphan_grace_seconds
                if stale:
                    logger.info("Cleanup: removing orphaned file %s", entry.name)
                    remove_file(entry)

        for directory in (self._settings.work_dir, self._settings.profile_dir):
            for entry in _iterdir(directory):
                if not entry.is_dir():
                    continue
                managed = is_uuid(entry.name) and entry.name not in active
                if (managed or entry.name.startswith("probe-")) and age(entry) > self.orphan_grace_seconds:
                    logger.info("Cleanup: removing orphaned directory %s", entry.name)
                    remove_tree(entry)


def _iterdir(directory: Path) -> list[Path]:
    try:
        return list(directory.iterdir())
    except OSError:
        return []
