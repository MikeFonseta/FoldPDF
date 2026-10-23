from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, parse_sqlite_url


def test_defaults_are_derived_from_data_dir(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]
    assert settings.host == "127.0.0.1"
    assert settings.port == 8080
    assert settings.max_upload_bytes == 26214400
    assert settings.upload_dir == tmp_path.resolve() / "uploads"
    assert settings.result_dir == tmp_path.resolve() / "results"
    assert settings.profile_dir == tmp_path.resolve() / "profiles"
    assert settings.database_path == tmp_path.resolve() / "jobs.db"


def test_values_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PORT", "9090")
    monkeypatch.setenv("MAX_WORKERS", "4")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("LIBREOFFICE_BIN", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.port == 9090
    assert settings.max_workers == 4
    assert settings.log_level == "DEBUG"
    assert settings.libreoffice_bin is None


def test_libreoffice_bin_quotes_are_removed(tmp_path: Path) -> None:
    value = r'"C:\Program Files\LibreOffice\program\soffice.exe"'
    settings = Settings(_env_file=None, data_dir=tmp_path, libreoffice_bin=value)  # type: ignore[call-arg]
    assert settings.libreoffice_bin == r"C:\Program Files\LibreOffice\program\soffice.exe"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite:///./data/jobs.db", Path("./data/jobs.db").resolve()),
        ("sqlite:///relative.db", Path("relative.db").resolve()),
    ],
)
def test_parse_sqlite_url(url: str, expected: Path) -> None:
    assert parse_sqlite_url(url) == expected


def test_parse_sqlite_url_absolute(tmp_path: Path) -> None:
    target = tmp_path / "db dir" / "jobs.db"
    assert parse_sqlite_url(f"sqlite:///{target.as_posix()}") == target.resolve()


@pytest.mark.parametrize("url", ["postgresql://host/db", "sqlite:///", "sqlite:///:memory:"])
def test_invalid_database_url(url: str, tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, data_dir=tmp_path, database_url=url)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "overrides",
    [
        {"port": 0},
        {"max_workers": 0},
        {"max_upload_bytes": 10},
        {"conversion_timeout_seconds": 0},
        {"log_level": "verbose"},
        {"host": "  "},
    ],
)
def test_invalid_values_are_rejected(overrides: dict[str, object], tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, data_dir=tmp_path, **overrides)  # type: ignore[call-arg]


def test_storage_dirs_must_be_distinct(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, data_dir=tmp_path, upload_dir=tmp_path / "x", result_dir=tmp_path / "x")  # type: ignore[call-arg]
