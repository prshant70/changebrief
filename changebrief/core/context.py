"""Shared runtime context for CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from logging import Logger
from typing import Any, Dict

import typer

from changebrief.core.exceptions import ConfigError


@dataclass
class AppContext:
    """Config and logger loaded once per invocation and attached to the root Typer context."""

    config: Dict[str, Any]
    logger: Logger


def require_app_context(ctx: typer.Context) -> AppContext:
    """Return :class:`AppContext` from the root command context."""
    root = ctx.find_root()
    obj = root.obj
    if obj is None or not isinstance(obj, AppContext):
        raise ConfigError("Application context is not initialized.")
    return obj
