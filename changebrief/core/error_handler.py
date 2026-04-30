"""Centralized CLI error handling."""

from __future__ import annotations

import logging
import traceback
from functools import wraps
from typing import Any, Callable, TypeVar

import click
import typer

from changebrief.core.exceptions import ConfigError, ChangeBriefError, ValidationError
from changebrief.core.exit_codes import ExitCodes

F = TypeVar("F", bound=Callable[..., Any])


def handle_errors(fn: F) -> F:
    """
    Wrap a Typer command: map exceptions to stderr messages and process exit codes.

    ``typer.Exit`` / ``SystemExit`` / ``KeyboardInterrupt`` are re-raised unchanged.
    Click usage errors (e.g. ``typer.BadParameter``) are also re-raised so the
    Typer/Click runtime can render the canonical "Invalid value: ..." message
    with its standard exit code (``2``).
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (typer.Exit, SystemExit, KeyboardInterrupt):
            raise
        except click.exceptions.UsageError:
            # Lets Typer/Click render a friendly message and exit with code 2.
            raise
        except ValidationError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(ExitCodes.VALIDATION_ERROR)
        except ConfigError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(ExitCodes.CONFIG_ERROR)
        except ChangeBriefError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(ExitCodes.UNKNOWN_ERROR)
        except Exception as exc:
            root = logging.getLogger()
            is_debug = root.isEnabledFor(logging.DEBUG)
            if is_debug:
                typer.secho(traceback.format_exc(), fg=typer.colors.RED, err=True)
            typer.secho(
                "An unexpected error occurred." + (" See traceback above." if is_debug else " Use --verbose for details."),
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(ExitCodes.UNKNOWN_ERROR)

    return wrapper  # type: ignore[return-value]
