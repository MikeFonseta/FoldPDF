"""Start with `python -m app`, using HOST, PORT and LOG_LEVEL from the configuration."""

from __future__ import annotations

import sys

import uvicorn
from pydantic import ValidationError

from app.config import get_settings


def main() -> int:
    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"Invalid configuration:\n{exc}", file=sys.stderr)
        return 2
    # A single process: the job queue lives in memory (see README, "Architecture").
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        workers=1,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
