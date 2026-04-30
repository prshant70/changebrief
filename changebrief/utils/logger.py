"""Application logging setup."""

import logging
import sys
from typing import Optional

_LOG_LEVEL_NAMES = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def setup_logging(level: Optional[str] = None) -> None:
    """Configure root logger for the CLI (stdout, simple format)."""
    name = (level or "INFO").upper()
    log_level = _LOG_LEVEL_NAMES.get(name, logging.INFO)
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(log_level)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(levelname)s: %(message)s"),
    )
    root.addHandler(handler)
    root.setLevel(log_level)


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger."""
    return logging.getLogger(name)
