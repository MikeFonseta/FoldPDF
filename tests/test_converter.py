"""Tests of the conversion engine with a simulated LibreOffice process (no installation needed)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from app.services import converter as converter_module
from app.services.converter import (
    ConversionFailedError,
    ConversionTimeoutError,
    EngineNotAvailableError,
    InvalidOutputError,
    LibreOfficeConverter,
)
from app.services.libreoffice import EngineStatus
from tests.conftest import MINIMAL_PDF


class StaticLocator:
    def __init__(self, executable: Path | None) -> None:
        self.status = EngineStatus(executable is not None, executable, "24.8", "", time.time())


class FakePopen:
    """Replacement for subprocess.Popen: simulates soffice by writing the PDF to --outdir."""

    behaviour = "success"
    instances: list[FakePopen] = []

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4_000_000
        self.returncode: int | None = None
        self.killed = False
        FakePopen.instances.append(self)

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        behaviour = FakePopen.behaviour
        if behaviour == "timeout" and not self.killed:
            raise subprocess.TimeoutExpired(self.argv, timeout or 0)
        out_dir = Path(self.argv[self.argv.index("--outdir") + 1])
        source = Path(self.argv[-1])
        target = out_dir / f"{source.stem}.pdf"
        self.returncode = 0
        if behaviour == "success":
            target.write_bytes(MINIMAL_PDF)
        elif behaviour == "exit_error":
            self.returncode = 77
        elif behaviour == "garbage":
            target.write_bytes(b"<html>" + b"x" * 200)
        elif behaviour == "truncated":
            target.write_bytes(b"%PDF-1.7\n" + b"x" * 200)
        return b"convert ok", b"warning: internal details"

    def kill(self) -> None:
        self.killed = True


@pytest.fixture
def fake_process(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    FakePopen.behaviour = "success"
    FakePopen.instances = []
    killed: list[str] = []
    monkeypatch.setattr(converter_module.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(converter_module, "_kill_process_tree", lambda process: killed.append(process.argv[0]) or process.kill())
    if hasattr(converter_module.os, "killpg"):
        monkeypatch.setattr(converter_module.os, "killpg", lambda pid, sig: None)
    return killed


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    result = {name: tmp_path / "with spaces" / name for name in ("work", "profiles", "uploads", "results")}
    for path in result.values():
        path.mkdir(parents=True)
    return result


def _converter(dirs: dict[str, Path], executable: Path | None = Path("/opt/Libre Office/soffice")) -> LibreOfficeConverter:
    return LibreOfficeConverter(StaticLocator(executable), dirs["work"], dirs["profiles"], timeout=3)


def _input(dirs: dict[str, Path], name: str = "job.docx") -> Path:
    path = dirs["uploads"] / name
    path.write_bytes(b"docx content")
    return path


def test_successful_conversion(dirs: dict[str, Path], fake_process: list[str]) -> None:
    output = dirs["results"] / "out.pdf"
    result = _converter(dirs).convert("job-1", _input(dirs), output)

    assert output.read_bytes() == MINIMAL_PDF
    assert result.size == len(MINIMAL_PDF)
    process = FakePopen.instances[0]
    argv = process.argv
    assert argv[0] == str(Path("/opt/Libre Office/soffice"))
    assert argv[1].startswith("-env:UserInstallation=file:")
    assert "with%20spaces" in argv[1]  # correctly encoded URI
    assert argv[argv.index("--convert-to") + 1] == "pdf"
    assert "--headless" in argv and "--norestore" in argv
    assert "shell" not in process.kwargs
    assert process.kwargs["stdin"] == subprocess.DEVNULL
    # intermediate files, profile and working directory removed
    assert list(dirs["work"].iterdir()) == []
    assert list(dirs["profiles"].iterdir()) == []
    assert not (dirs["results"] / "out.pdf.tmp").exists()
    assert fake_process == []


def test_each_job_has_its_own_profile_and_workdir(dirs: dict[str, Path], fake_process: list[str]) -> None:
    converter = _converter(dirs)
    converter.convert("job-a", _input(dirs, "a.docx"), dirs["results"] / "a.pdf")
    converter.convert("job-b", _input(dirs, "b.xlsx"), dirs["results"] / "b.pdf")
    first, second = FakePopen.instances
    assert first.argv[1] != second.argv[1]
    assert "job-a" in first.argv[1] and "job-b" in second.argv[1]
    assert first.kwargs["cwd"] != second.kwargs["cwd"]
    assert Path(second.argv[-1]).name == "document.xlsx"  # internal name, never the client's


def test_nonzero_exit_code(dirs: dict[str, Path], fake_process: list[str]) -> None:
    FakePopen.behaviour = "exit_error"
    with pytest.raises(ConversionFailedError) as excinfo:
        _converter(dirs).convert("job", _input(dirs), dirs["results"] / "out.pdf")
    assert "internal details" not in excinfo.value.public_message
    assert not (dirs["results"] / "out.pdf").exists()
    assert list(dirs["work"].iterdir()) == []


def test_missing_output(dirs: dict[str, Path], fake_process: list[str]) -> None:
    FakePopen.behaviour = "no_output"
    with pytest.raises(ConversionFailedError, match="did not produce"):
        _converter(dirs).convert("job", _input(dirs), dirs["results"] / "out.pdf")


@pytest.mark.parametrize("behaviour", ["garbage", "truncated"])
def test_implausible_pdf(dirs: dict[str, Path], fake_process: list[str], behaviour: str) -> None:
    FakePopen.behaviour = behaviour
    with pytest.raises(InvalidOutputError):
        _converter(dirs).convert("job", _input(dirs), dirs["results"] / "out.pdf")
    assert not (dirs["results"] / "out.pdf").exists()


def test_timeout_kills_process_tree(dirs: dict[str, Path], fake_process: list[str]) -> None:
    FakePopen.behaviour = "timeout"
    with pytest.raises(ConversionTimeoutError, match="3 seconds"):
        _converter(dirs).convert("job", _input(dirs), dirs["results"] / "out.pdf")
    assert len(fake_process) == 1
    assert FakePopen.instances[0].killed
    assert list(dirs["work"].iterdir()) == []
    assert list(dirs["profiles"].iterdir()) == []


def test_engine_unavailable(dirs: dict[str, Path], fake_process: list[str]) -> None:
    with pytest.raises(EngineNotAvailableError):
        _converter(dirs, executable=None).convert("job", _input(dirs), dirs["results"] / "out.pdf")
    assert FakePopen.instances == []


def test_executable_cannot_be_started(dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_popen(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("soffice")

    monkeypatch.setattr(converter_module.subprocess, "Popen", broken_popen)
    with pytest.raises(EngineNotAvailableError):
        _converter(dirs).convert("job", _input(dirs), dirs["results"] / "out.pdf")
    assert list(dirs["work"].iterdir()) == []


def test_real_process_tree_is_killed_on_timeout(tmp_path: Path) -> None:
    """Uses a real (Python) process that spawns a grandchild: both must be terminated on timeout."""
    marker = tmp_path / "grandchild-alive"
    grandchild = f"import time, pathlib; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('x')"
    child = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(60)"
    )
    started = time.monotonic()
    with pytest.raises(ConversionTimeoutError):
        converter_module.run_process([sys.executable, "-c", child], cwd=tmp_path, env=dict(os.environ), timeout=1)
    assert time.monotonic() - started < 20
    time.sleep(4)
    assert not marker.exists(), "the grandchild process survived the timeout"


def test_verify_pdf_rejects_tiny_file(tmp_path: Path) -> None:
    path = tmp_path / "x.pdf"
    path.write_bytes(b"%PDF-1.4 %%EOF")
    with pytest.raises(InvalidOutputError):
        converter_module.verify_pdf(path)
