"""changebrief cache — inspect and purge local cache."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from changebrief.core.cache.store import DEFAULT_TTL_S, get_cache_root, list_cache_items, purge_cache
from changebrief.core.context import require_app_context
from changebrief.core.error_handler import handle_errors

cache_app = typer.Typer(
    help="Inspect and purge the local validation cache.",
    epilog="Examples:\n  changebrief cache list\n  changebrief cache purge --expired\n  changebrief cache purge --all",
)


@cache_app.callback()
@handle_errors
def _cache_root(ctx: typer.Context) -> None:
    _ = require_app_context(ctx)


@cache_app.command("list")
@handle_errors
def cache_list(
    ctx: typer.Context,
    repo_id: Optional[str] = typer.Option(None, "--repo-id", help="Filter by repo id."),
) -> None:
    """
    List cached artifacts.
    """
    _ = require_app_context(ctx)
    rows = list_cache_items()
    if repo_id:
        rows = [r for r in rows if r.get("repo_id") == repo_id]

    root = get_cache_root()
    typer.echo(f"Cache root: {root}")
    if not rows:
        typer.echo("(no cache entries found)")
        return

    # compact stable display
    for r in rows:
        typer.echo(f"- {r['repo_id']} {r['pair']} {r['version']} {r['key']}")


@cache_app.command("purge")
@handle_errors
def cache_purge(
    ctx: typer.Context,
    all: bool = typer.Option(False, "--all", help="Delete all cache entries."),
    expired: bool = typer.Option(False, "--expired", help="Delete only expired entries (TTL-based)."),
    repo_id: Optional[str] = typer.Option(None, "--repo-id", help="Delete cache for a specific repo id."),
) -> None:
    """
    Purge cache entries.
    """
    _ = require_app_context(ctx)

    if all and expired:
        raise typer.BadParameter("Use only one of --all or --expired.")
    if not all and not expired and not repo_id:
        raise typer.BadParameter("Specify --all, --expired, or --repo-id.")

    deleted = purge_cache(
        repo_id=None if all else repo_id,
        expired_only=expired,
        ttl_s=DEFAULT_TTL_S,
    )
    typer.echo(f"Deleted {deleted} cache file(s).")

