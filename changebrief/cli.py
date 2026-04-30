"""ChangeBrief CLI entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
import yaml

from changebrief import __version__ as PKG_VERSION
from changebrief.commands.ai_context import ai_context_app
from changebrief.commands.cache import cache_app
from changebrief.commands.validate import validate_app
from changebrief.core.config import (
    DEFAULTS,
    load_config,
    mask_api_key,
    save_config,
    validate_config_values,
    validate_llm_provider_input,
)
from changebrief.core.context import AppContext, require_app_context
from changebrief.core.error_handler import handle_errors
from changebrief.core.exceptions import ConfigError
from changebrief.core.exit_codes import ExitCodes
from changebrief.core.validator import validate_config_present
from changebrief.utils.logger import get_logger, setup_logging
from changebrief.utils.paths import get_config_dir, get_config_file

app = typer.Typer(
    name="changebrief",
    help="🔧 ChangeBrief — change-aware validation assistant for code diffs.",
    epilog="Run `changebrief <command> --help` for usage. Quickstart: `changebrief init`",
    add_completion=False,
)
app.add_typer(validate_app, name="validate")
app.add_typer(cache_app, name="cache")
app.add_typer(ai_context_app, name="ai-context")

config_app = typer.Typer(
    help="Inspect or manage configuration.",
    epilog="Examples:\n  changebrief config show\n  changebrief config check",
)
app.add_typer(config_app, name="config")


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"changebrief {PKG_VERSION}")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose logging to stderr.",
    ),
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        help="Show version and exit.",
        callback=_print_version,
        is_eager=True,
    ),
) -> None:
    """ChangeBrief root: load config once, configure logging, attach :class:`AppContext`."""
    _ = version
    try:
        cfg = load_config()
    except ConfigError as exc:
        cfg = dict(DEFAULTS)
        setup_logging("INFO")
        get_logger(__name__).warning("Using default config: %s", exc)
    else:
        level = str(cfg.get("log_level", "INFO"))
        if verbose:
            level = "DEBUG"
        setup_logging(level)

    log = get_logger("changebrief")
    ctx.obj = AppContext(config=cfg, logger=log)


@config_app.command("show")
@handle_errors
def config_show(
    ctx: typer.Context,
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to config file (defaults to ~/.changebrief/config.yaml).",
        exists=True,
        file_okay=True,
        dir_okay=False,
    ),
) -> None:
    """Print the current configuration (API key masked)."""
    _ = require_app_context(ctx)
    data = load_config(config)
    safe = dict(data)
    safe["llm_api_key"] = mask_api_key(str(safe.get("llm_api_key", "")))
    typer.echo(yaml.safe_dump(safe, default_flow_style=False, sort_keys=True).rstrip())


@config_app.command("check")
@handle_errors
def config_check(
    ctx: typer.Context,
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to config file (defaults to ~/.changebrief/config.yaml).",
        exists=True,
        file_okay=True,
        dir_okay=False,
    ),
) -> None:
    """
    Verify the config file exists, load it, and validate known fields.

    Prints current values (API key masked) and any warnings. Exits with a
    non-zero code if the file is missing or values are invalid.
    """
    _ = require_app_context(ctx)
    path = validate_config_present(config)
    data = load_config(config)
    warnings = validate_config_values(data)

    typer.echo(typer.style(f"Config file: {path}", bold=True))
    typer.echo("")
    display = dict(data)
    display["llm_api_key"] = mask_api_key(str(display.get("llm_api_key", "")))
    for key in sorted(display.keys()):
        typer.echo(f"  {key}: {display[key]}")
    for msg in warnings:
        typer.echo("")
        typer.echo(typer.style(f"⚠ {msg}", fg=typer.colors.YELLOW), err=True)
    typer.echo("")
    typer.echo(typer.style("✓ Configuration is valid.", fg=typer.colors.GREEN))


@app.command()
@handle_errors
def init(
    ctx: typer.Context,
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing config file.",
    ),
) -> None:
    """
    Create ~/.changebrief/ and write config.yaml interactively.

    Prompts for LLM provider, optional default model, and API key (hidden).
    """
    _ = require_app_context(ctx)
    cfg_dir = get_config_dir()
    cfg_path = get_config_file()
    cfg_dir.mkdir(parents=True, exist_ok=True)

    if cfg_path.exists() and not force:
        typer.echo(
            typer.style(
                f"Config already exists at {cfg_path}. Use --force to replace.",
                fg=typer.colors.YELLOW,
            ),
            err=True,
        )
        raise typer.Exit(ExitCodes.UNKNOWN_ERROR)

    typer.echo("Welcome to ChangeBrief — let's set up your config (stored under ~/.changebrief/).")
    raw_provider = typer.prompt(
        "LLM provider",
        default=DEFAULTS["llm_provider"],
        show_default=True,
    )
    provider = validate_llm_provider_input(raw_provider)

    model_default = DEFAULTS["default_model"]
    model = typer.prompt(
        "Default model",
        default=model_default,
        show_default=True,
    ).strip() or model_default

    api_key = typer.prompt("API key", hide_input=True, default="")

    data = dict(DEFAULTS)
    data["llm_provider"] = provider
    data["default_model"] = model
    data["llm_api_key"] = api_key

    out_path = save_config(data)
    if not out_path.is_file():
        typer.echo(typer.style("Failed to create config file.", fg=typer.colors.RED), err=True)
        raise typer.Exit(ExitCodes.UNKNOWN_ERROR)

    merged = load_config()
    for msg in validate_config_values(merged):
        typer.echo(typer.style(f"⚠ {msg}", fg=typer.colors.YELLOW), err=True)

    get_logger(__name__).info("Wrote configuration to %s", out_path)
    typer.echo(typer.style(f"✓ Config saved to {out_path}", fg=typer.colors.GREEN))


def main() -> None:
    """Console script entrypoint."""
    try:
        app()
    except typer.Exit as exc:
        code = exc.exit_code
        if code is None:
            code = ExitCodes.SUCCESS
        sys.exit(code)


if __name__ == "__main__":
    main()
