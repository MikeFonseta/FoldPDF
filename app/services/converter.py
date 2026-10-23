"""Conversion engine based on headless LibreOffice, independent of the REST endpoints.

For each conversion it:
1. creates an isolated working directory (`WORK_DIR/<job_id>`) with `in/`, `out/` and `tmp/`;
2. creates a dedicated LibreOffice profile (`PROFILE_DIR/<job_id>`), never shared;
3. starts LibreOffice with separate arguments and no shell;
4. enforces the timeout, killing the whole process tree when it expires;
5. checks the exit code and the plausibility of the PDF;
6. moves the PDF to its destination atomically;
7. removes the working directory and the profile.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app.services.libreoffice import LibreOfficeLocator
from app.services.storage import make_private, remove_tree

logger = logging.getLogger(__name__)

MIN_PDF_BYTES = 64
_LOG_OUTPUT_LIMIT = 2000
_IS_WINDOWS = os.name == "nt"


class ConversionError(Exception):
    """Conversion error carrying a message that is safe to expose to the client."""

    def __init__(self, public_message: str) -> None:
        self.public_message = public_message
        super().__init__(public_message)


class EngineNotAvailableError(ConversionError):
    pass


class ConversionTimeoutError(ConversionError):
    pass


class ConversionFailedError(ConversionError):
    pass


class InvalidOutputError(ConversionError):
    pass


@dataclass(frozen=True)
class ConversionResult:
    size: int
    duration: float


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: int
    stdout: str
    stderr: str


def build_command(executable: Path, profile_dir: Path, out_dir: Path, source: Path) -> list[str]:
    return [
        str(executable),
        f"-env:UserInstallation={profile_dir.as_uri()}",
        "--headless",
        "--invisible",
        "--nodefault",
        "--nofirststartwizard",
        "--nolockcheck",
        "--nologo",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir),
        str(source),
    ]


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate the process and all of its descendants (soffice spawns child processes)."""
    if _IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("taskkill failed for PID %s: %s", process.pid, exc)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")[-_LOG_OUTPUT_LIMIT:]


def run_process(argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> ProcessOutcome:
    """Run a command without a shell, in a new process group, with a timeout."""
    kwargs: dict[str, object] = {}
    if _IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(  # noqa: S603 - explicit arguments, no shell
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            process.communicate(timeout=10)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        raise ConversionTimeoutError(f"The conversion exceeded the maximum time of {timeout:g} seconds.") from None
    finally:
        if not _IS_WINDOWS:
            # Kill any leftover process of the group, even after a regular exit.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    return ProcessOutcome(process.returncode, _decode(stdout), _decode(stderr))


def verify_pdf(path: Path) -> int:
    """Check that the file is a plausible PDF and return its size."""
    size = path.stat().st_size
    if size < MIN_PDF_BYTES:
        raise InvalidOutputError("The generated PDF is empty or incomplete.")
    with path.open("rb") as fh:
        head = fh.read(1024)
        fh.seek(max(0, size - 2048))
        tail = fh.read()
    if not head.lstrip().startswith(b"%PDF-") or b"%%EOF" not in tail:
        raise InvalidOutputError("The generated PDF is not valid.")
    return size


class LibreOfficeConverter:
    def __init__(self, locator: LibreOfficeLocator, work_dir: Path, profile_dir: Path, timeout: float) -> None:
        self._locator = locator
        self._work_dir = work_dir
        self._profile_dir = profile_dir
        self._timeout = timeout

    def convert(self, job_id: str, input_path: Path, output_path: Path) -> ConversionResult:
        status = self._locator.status
        if not status.available or status.executable is None:
            raise EngineNotAvailableError("The LibreOffice conversion engine is not available.")

        workdir = self._work_dir / job_id
        profile = self._profile_dir / job_id
        in_dir, out_dir, tmp_dir = workdir / "in", workdir / "out", workdir / "tmp"
        started = time.monotonic()
        try:
            for directory in (workdir, profile):
                remove_tree(directory)
            for directory in (workdir, in_dir, out_dir, tmp_dir, profile):
                directory.mkdir(parents=True, exist_ok=True)
                make_private(directory)

            source = in_dir / f"document{input_path.suffix.lower()}"
            shutil.copyfile(input_path, source)

            env = dict(os.environ)
            env.update({"TMPDIR": str(tmp_dir), "TMP": str(tmp_dir), "TEMP": str(tmp_dir)})
            argv = build_command(status.executable, profile, out_dir, source)
            logger.debug("Job %s: starting LibreOffice %s", job_id, argv)

            try:
                outcome = run_process(argv, cwd=workdir, env=env, timeout=self._timeout)
            except OSError as exc:
                logger.error("Job %s: cannot start LibreOffice: %s", job_id, exc)
                raise EngineNotAvailableError("The conversion engine cannot be started.") from exc
            produced = out_dir / f"{source.stem}.pdf"
            if outcome.returncode != 0:
                logger.warning(
                    "Job %s: LibreOffice exited with code %s. stdout=%r stderr=%r",
                    job_id, outcome.returncode, outcome.stdout, outcome.stderr,
                )
                raise ConversionFailedError("LibreOffice could not convert the document.")
            if not produced.is_file():
                logger.warning("Job %s: no PDF produced. stdout=%r stderr=%r", job_id, outcome.stdout, outcome.stderr)
                raise ConversionFailedError(
                    "LibreOffice did not produce a PDF: the document may be corrupted or unsupported."
                )
            size = verify_pdf(produced)

            staging = output_path.with_name(output_path.name + ".tmp")
            shutil.move(produced, staging)
            make_private(staging)
            os.replace(staging, output_path)
            return ConversionResult(size=size, duration=time.monotonic() - started)
        finally:
            remove_tree(workdir)
            remove_tree(profile)
