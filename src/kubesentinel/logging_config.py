"""Structured JSON logging setup."""

from __future__ import annotations

import logging
import sys

from pythonjsonlogger import jsonlogger


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level"},
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # The kubernetes client is chatty at DEBUG; keep it at WARNING unless explicitly requested.
    if level.upper() != "DEBUG":
        logging.getLogger("kubernetes").setLevel(logging.WARNING)
