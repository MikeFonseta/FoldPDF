"""Application logging configuration."""

from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(level: str) -> None:
    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        app_logger.addHandler(handler)
        app_logger.propagate = False
