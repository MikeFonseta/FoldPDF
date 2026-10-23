"""Locating and probing the LibreOffice executable.

Search order:
1. `LIBREOFFICE_BIN` (full path or name of an executable on PATH). When set, no
   other locations are searched: a wrong value is a configuration error and
   must be reported, not hidden.
2. `soffice` / `libreoffice` on `PATH` (standard resolution, including PATHEXT on Windows).
3. Standard installation paths on Windows, macOS and Linux.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 60
_EXECUTABLE_NAMES = ("soffice", "libreoffice")
_VERSION_RE = re.compile(r"LibreOffice\s+(\d+(?:\.\d+)+)")


def _platform_candidates() -> list[Path]:
    if sys.platform == "win32":
        roots = {
            os.environ.get(var)
            for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA")
        }
        candidates = []
        for root in sorted(r for r in roots if r):
            for folder in ("LibreOffice", "Programs\\LibreOffice"):
                candidates.append(Path(root) / folder / "program" / "soffice.exe")
        return candidates
    if sys.platform == "darwin":
        return [
            Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
            Path.home() / "Applications/LibreOffice.app/Contents/MacOS/soffice",
        ]
    candidates = [
        Path("/usr/bin/soffice"),
        Path("/usr/lib/libreoffice/program/soffice"),
        Path("/usr/lib64/libreoffice/program/soffice"),
        Path("/usr/local/bin/soffice"),
        Path("/snap/bin/libreoffice"),
    ]
    candidates.extend(sorted(Path("/opt").glob("libreoffice*/program/soffice")))
    return candidates


def _is_executable(path: Path) -> bool:
    return path.is_file() and (sys.platform == "win32" or os.access(path, os.X_OK))


def _resolve_configured(value: str) -> Path | None:
    path = Path(value).expanduser()
    if path.is_dir():
        for name in ("soffice.exe", "soffice", "program/soffice.exe", "program/soffice"):
            if _is_executable(path / name):
                return (path / name).resolve()
        return None
    if _is_executable(path):
        return path.resolve()
    found = shutil.which(value)
    return Path(found).resolve() if found else None


def find_libreoffice(configured: str | None) -> Path | None:
    """Return the path of the LibreOffice executable, or `None` if not found."""
    if configured:
        return _resolve_configured(configured)
    for name in _EXECUTABLE_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    for candidate in _platform_candidates():
        if _is_executable(candidate):
            return candidate.resolve()
    return None


@dataclass(frozen=True)
class EngineStatus:
    available: bool
    executable: Path | None
    version: str | None
    message: str
    checked_at: float


class LibreOfficeLocator:
    """Keeps track of the conversion engine status, verified with `--version`."""

    def __init__(self, configured: str | None, probe_profile_dir: Path) -> None:
        self._configured = configured
        self._probe_profile_dir = probe_profile_dir
        self._lock = threading.Lock()
        self._status = EngineStatus(False, None, None, "Engine not checked yet.", 0.0)

    @property
    def status(self) -> EngineStatus:
        return self._status

    def detect(self) -> EngineStatus:
        with self._lock:
            self._status = self._detect()
        status = self._status
        if status.available:
            logger.info("LibreOffice available: %s (version %s)", status.executable, status.version or "n/a")
        else:
            logger.error("LibreOffice NOT available: %s", status.message)
        return status

    def refresh_if_stale(self, max_age: float = 30.0) -> EngineStatus:
        status = self._status
        if status.available or time.time() - status.checked_at < max_age:
            if status.available and status.executable and not status.executable.exists():
                return self.detect()
            return status
        return self.detect()

    def _detect(self) -> EngineStatus:
        now = time.time()
        executable = find_libreoffice(self._configured)
        if executable is None:
            if self._configured:
                message = "LIBREOFFICE_BIN does not point to a valid LibreOffice executable."
            else:
                message = (
                    "LibreOffice was not found on PATH or in the standard locations. "
                    "Install it or set LIBREOFFICE_BIN."
                )
            return EngineStatus(False, None, None, message, now)
        return self._probe(executable, now)

    def _probe(self, executable: Path, now: float) -> EngineStatus:
        self._probe_profile_dir.mkdir(parents=True, exist_ok=True)
        profile = Path(tempfile.mkdtemp(prefix="probe-", dir=self._probe_profile_dir))
        argv = [str(executable), f"-env:UserInstallation={profile.as_uri()}", "--headless", "--version"]
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return EngineStatus(False, executable, None, "LibreOffice did not answer the version check.", now)
        except OSError as exc:
            logger.error("Cannot run %s: %s", executable, exc)
            return EngineStatus(False, executable, None, "LibreOffice cannot be executed.", now)
        finally:
            shutil.rmtree(profile, ignore_errors=True)

        output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
        if completed.returncode != 0:
            logger.error("LibreOffice check failed (exit %s): %s", completed.returncode, output[:500])
            return EngineStatus(False, executable, None, "LibreOffice is installed but does not work properly.", now)
        match = _VERSION_RE.search(output)
        return EngineStatus(True, executable, match.group(1) if match else None, "LibreOffice available.", now)
