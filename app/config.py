"""Centralized configuration, read from environment variables and the `.env` file.

Values are validated at startup: an invalid configuration prevents the service
from starting and produces an explicit error message.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
_SQLITE_PREFIX = "sqlite:///"
MIN_API_KEY_LENGTH = 16
_DIR_FIELDS = {
    "upload_dir": "uploads",
    "result_dir": "results",
    "profile_dir": "profiles",
    "work_dir": "work",
}


def parse_sqlite_url(url: str) -> Path:
    """Convert a `sqlite:///path` URL into an absolute path.

    - `sqlite:///./data/jobs.db`  -> path relative to the current directory
    - `sqlite:////data/jobs.db`   -> absolute POSIX path `/data/jobs.db`
    - `sqlite:///C:/data/jobs.db` -> absolute Windows path
    """
    if not url.startswith(_SQLITE_PREFIX):
        raise ValueError("DATABASE_URL must start with 'sqlite:///' (e.g. sqlite:///./data/jobs.db)")
    raw = url[len(_SQLITE_PREFIX):].split("?", 1)[0]
    if not raw or raw == ":memory:":
        raise ValueError("DATABASE_URL must point to an SQLite file on disk")
    return Path(raw).expanduser().resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)

    data_dir: Path = Path("data")
    database_url: str | None = None
    upload_dir: Path | None = None
    result_dir: Path | None = None
    profile_dir: Path | None = None
    work_dir: Path | None = None

    libreoffice_bin: str | None = None

    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1024, le=2 * 1024**3)
    max_workers: int = Field(default=2, ge=1, le=32)
    max_queued_jobs: int = Field(default=100, ge=1)
    max_attempts: int = Field(default=2, ge=1, le=10)
    conversion_timeout_seconds: float = Field(default=120, gt=0, le=3600)
    job_ttl_seconds: int = Field(default=3600, ge=1)
    cleanup_interval_seconds: int = Field(default=60, ge=1)

    api_keys: str = ""
    max_sync_wait_seconds: int = Field(default=30, ge=0, le=300)

    zip_max_entries: int = Field(default=10_000, ge=10)
    zip_max_uncompressed_bytes: int = Field(default=512 * 1024**2, ge=1024**2)
    zip_max_compression_ratio: int = Field(default=200, ge=10)

    log_level: str = "INFO"

    @field_validator("database_url", "libreoffice_bin", *_DIR_FIELDS, mode="before")
    @classmethod
    def _empty_as_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("libreoffice_bin")
    @classmethod
    def _strip_quotes(cls, value: str | None) -> str | None:
        # On Windows paths are often copied together with their quotes.
        return value.strip().strip('"').strip("'") if value else value

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str | None) -> str | None:
        if value is not None:
            parse_sqlite_url(value)
        return value

    @field_validator("api_keys")
    @classmethod
    def _check_api_keys(cls, value: str) -> str:
        keys = [key.strip() for key in value.split(",") if key.strip()]
        if any(len(key) < MIN_API_KEY_LENGTH for key in keys):
            raise ValueError(f"every key in API_KEYS must be at least {MIN_API_KEY_LENGTH} characters long")
        return ",".join(keys)

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("HOST must not be empty")
        return value

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in _LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(_LOG_LEVELS)}")
        return value

    @model_validator(mode="after")
    def _resolve_paths(self) -> Settings:
        self.data_dir = self.data_dir.expanduser().resolve()
        for name, default in _DIR_FIELDS.items():
            value: Path | None = getattr(self, name)
            setattr(self, name, (value.expanduser() if value else self.data_dir / default).resolve())
        dirs = [getattr(self, name) for name in _DIR_FIELDS]
        if len(set(dirs)) != len(dirs):
            raise ValueError("UPLOAD_DIR, RESULT_DIR, PROFILE_DIR and WORK_DIR must be distinct directories")
        return self

    @property
    def database_path(self) -> Path:
        if self.database_url is None:
            return self.data_dir / "jobs.db"
        return parse_sqlite_url(self.database_url)

    @property
    def api_key_list(self) -> tuple[str, ...]:
        """Accepted API keys; an empty tuple means authentication is disabled."""
        return tuple(key for key in self.api_keys.split(",") if key)

    @property
    def storage_dirs(self) -> dict[str, Path]:
        return {name: getattr(self, name) for name in _DIR_FIELDS}


@lru_cache
def get_settings() -> Settings:
    return Settings()
