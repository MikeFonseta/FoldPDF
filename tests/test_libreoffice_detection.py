from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.services import libreoffice
from app.services.libreoffice import LibreOfficeLocator, find_libreoffice

EXE_NAME = "soffice.exe" if sys.platform == "win32" else "soffice"


def _fake_executable(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / EXE_NAME
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def no_system_libreoffice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(libreoffice.shutil, "which", lambda name: None)
    monkeypatch.setattr(libreoffice, "_platform_candidates", lambda: [])


def test_configured_full_path_with_spaces(tmp_path: Path, no_system_libreoffice: None) -> None:
    exe = _fake_executable(tmp_path / "Program Files" / "Libre Office" / "program")
    assert find_libreoffice(str(exe)) == exe.resolve()


def test_configured_directory_is_searched(tmp_path: Path, no_system_libreoffice: None) -> None:
    exe = _fake_executable(tmp_path / "LibreOffice" / "program")
    assert find_libreoffice(str(tmp_path / "LibreOffice")) == exe.resolve()


def test_configured_invalid_path_does_not_fall_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = _fake_executable(tmp_path / "other")
    monkeypatch.setattr(libreoffice, "_platform_candidates", lambda: [other])
    monkeypatch.setattr(libreoffice.shutil, "which", lambda name: None)
    assert find_libreoffice(str(tmp_path / "missing" / EXE_NAME)) is None


def test_configured_command_name_resolved_via_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _fake_executable(tmp_path / "bin")
    monkeypatch.setattr(libreoffice.shutil, "which", lambda name: str(exe) if name == "lo-custom" else None)
    assert find_libreoffice("lo-custom") == exe.resolve()


def test_found_in_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _fake_executable(tmp_path / "bin")
    monkeypatch.setattr(libreoffice.shutil, "which", lambda name: str(exe) if name == "soffice" else None)
    assert find_libreoffice(None) == exe.resolve()


def test_found_in_standard_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _fake_executable(tmp_path / "opt" / "libreoffice" / "program")
    monkeypatch.setattr(libreoffice.shutil, "which", lambda name: None)
    monkeypatch.setattr(libreoffice, "_platform_candidates", lambda: [tmp_path / "nope" / EXE_NAME, exe])
    assert find_libreoffice(None) == exe.resolve()


def test_not_found(no_system_libreoffice: None) -> None:
    assert find_libreoffice(None) is None


def test_locator_reports_missing_engine(tmp_path: Path, no_system_libreoffice: None) -> None:
    status = LibreOfficeLocator(None, tmp_path).detect()
    assert status.available is False
    assert "LIBREOFFICE_BIN" in status.message


def test_locator_reports_invalid_configuration(tmp_path: Path, no_system_libreoffice: None) -> None:
    status = LibreOfficeLocator(str(tmp_path / "missing"), tmp_path).detect()
    assert status.available is False
    assert "LIBREOFFICE_BIN" in status.message


def test_locator_probe_success_without_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _fake_executable(tmp_path / "Program Files" / "LibreOffice")
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"LibreOffice 24.8.4.2 480(Build:2)\n", b"")

    monkeypatch.setattr(libreoffice.subprocess, "run", fake_run)
    status = LibreOfficeLocator(str(exe), tmp_path / "profiles").detect()

    assert status.available is True
    assert status.version == "24.8.4.2"
    argv, kwargs = calls[0]
    assert argv[0] == str(exe.resolve())  # a path with spaces is passed as a single argument
    assert "--version" in argv
    assert any(arg.startswith("-env:UserInstallation=file:") for arg in argv)
    assert "shell" not in kwargs
    assert list((tmp_path / "profiles").iterdir()) == []  # probe profile removed


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess([], 1, b"", b"boom"),
        subprocess.TimeoutExpired(cmd="soffice", timeout=1),
        OSError("exec format error"),
    ],
)
def test_locator_probe_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: object) -> None:
    exe = _fake_executable(tmp_path / "bin")

    def fake_run(argv: list[str], **kwargs: object) -> object:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(libreoffice.subprocess, "run", fake_run)
    status = LibreOfficeLocator(str(exe), tmp_path / "profiles").detect()
    assert status.available is False
    assert status.executable == exe.resolve()
