"""changebrief ai-context — generate per-agent context files."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import typer

from changebrief.core.ai_context.composer import compose_context
from changebrief.core.ai_context.config_loader import load_context_config
from changebrief.core.ai_context.enricher import enrich
from changebrief.core.ai_context.generator import (
    AGENT_TARGETS,
    AgentTarget,
    has_marker,
    merge_with_existing,
    render,
)
from changebrief.core.ai_context.scanner import scan_repo
from changebrief.core.context import require_app_context
from changebrief.core.error_handler import handle_errors
from changebrief.core.exit_codes import ExitCodes


ai_context_app = typer.Typer(
    help="Generate per-agent context files (CLAUDE.md, CURSOR.md, CODEX.md).",
    epilog=(
        "Examples:\n"
        "  changebrief ai-context init\n"
        "  changebrief ai-context init --dry-run\n"
        "  changebrief ai-context init --targets claude --targets cursor"
    ),
)


@ai_context_app.callback()
@handle_errors
def _ai_context_root(ctx: typer.Context) -> None:
    _ = require_app_context(ctx)


def _undescribed_major_imports(repo_ctx, cfg) -> list[tuple[str, int]]:
    """Return ``[(pkg, file_count)]`` for major imports without a friendly name.

    Used by the CLI to nudge users toward the ``frameworks:`` config override.
    """
    overrides = {k.lower() for k in (cfg.frameworks or {})}
    out: list[tuple[str, int]] = []
    for profile in repo_ctx.profiles:
        if profile.language == "generic":
            continue
        curated = {ev.fact.lower() for ev in profile.frameworks}
        for pkg, count in profile.major_imports.items():
            if count < 3:
                continue
            if pkg in overrides:
                continue
            if pkg.lower() in curated:
                continue
            out.append((pkg, count))
    out.sort(key=lambda kv: (-kv[1], kv[0]))
    return out


def _normalise_targets(values: List[str]) -> List[AgentTarget]:
    out: List[AgentTarget] = []
    seen: set[str] = set()
    for raw in values:
        key = raw.strip().lower()
        if key not in AGENT_TARGETS:
            raise typer.BadParameter(
                f"Unknown target {raw!r}. Choose from: {', '.join(sorted(AGENT_TARGETS))}."
            )
        if key in seen:
            continue
        seen.add(key)
        out.append(AGENT_TARGETS[key])
    return out


@ai_context_app.command("init")
@handle_errors
def init_cmd(
    ctx: typer.Context,
    path: Path = typer.Option(
        Path("."),
        "--path",
        "-p",
        help="Repository root to scan (defaults to current directory).",
        file_okay=False,
        dir_okay=True,
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing files even when they have no changebrief markers.",
    ),
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to context config (defaults to <repo>/.changebrief/context.yaml).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be written and exit. No files are touched.",
    ),
    targets: List[str] = typer.Option(
        ["claude", "cursor", "codex"],
        "--targets",
        "-t",
        help="Which agent context files to generate (repeatable).",
    ),
    enrich_with_llm: bool = typer.Option(
        False,
        "--enrich",
        help=(
            "Augment the deterministic output with an LLM pass that adds an inferred "
            "conventions and gotchas section. Each LLM-suggested item is verified to "
            "cite a real file in the repo; unverifiable items are dropped. Off by default."
        ),
    ),
) -> None:
    """
    Scan the repo, compose an evidence-backed context, and write the per-agent files.

    Each generated file is wrapped in safe `<!-- changebrief:ai-context:start ... -->`
    markers so re-running the command updates only the generated section and
    preserves any hand-edited content above or below it.
    """
    app_ctx = require_app_context(ctx)
    repo_path = path.resolve()

    selected = _normalise_targets(targets)

    repo_ctx = scan_repo(repo_path)
    cfg = load_context_config(repo_path, explicit_path=config)
    ai_ctx = compose_context(repo_ctx, cfg)

    typer.echo(typer.style(f"Scanned {repo_path}", bold=True))
    typer.echo(
        f"  primary language: {repo_ctx.primary_language or 'unknown'} "
        f"({repo_ctx.repo_size_files} files seen)"
    )
    if repo_ctx.profiles:
        fws = sorted({e.fact for p in repo_ctx.profiles for e in p.frameworks})
        if fws:
            typer.echo(f"  frameworks: {', '.join(fws[:8])}")

    # Surface heavily-imported, undescribed packages so users know they can
    # teach the tool about internal frameworks via `.changebrief/context.yaml`.
    unknowns = _undescribed_major_imports(repo_ctx, cfg)
    if unknowns:
        typer.echo(
            typer.style(
                "  tip: heavily-imported packages without a description: "
                + ", ".join(f"{pkg} ({n} files)" for pkg, n in unknowns[:5])
                + ". Add them to `.changebrief/context.yaml` under `frameworks:` so "
                "agents see a meaningful name (e.g. `torpedo: \"Torpedo (Sanic wrapper)\"`).",
                fg=typer.colors.CYAN,
            )
        )

    if enrich_with_llm:
        ai_ctx, enrich_result = enrich(ai_ctx, repo_ctx, config=app_ctx.config)
        if enrich_result.used_llm:
            typer.echo(
                typer.style(
                    f"  enriched via LLM"
                    + (" (cache hit)" if enrich_result.cache_hit else "")
                    + f": kept {enrich_result.convs_kept} conventions ({enrich_result.convs_dropped} dropped),"
                    + f" {enrich_result.gotchas_kept} gotchas ({enrich_result.gotchas_dropped} dropped)",
                    fg=typer.colors.CYAN,
                )
            )
        else:
            typer.echo(
                typer.style(
                    f"  enrichment skipped: {enrich_result.reason_skipped}",
                    fg=typer.colors.YELLOW,
                ),
                err=True,
            )
    typer.echo("")

    wrote = 0
    skipped: list[str] = []
    for target in selected:
        rendered = render(ai_ctx, target=target)
        out_path = repo_path / target.filename

        if dry_run:
            typer.echo(typer.style(f"--- {target.filename} (dry-run) ---", bold=True))
            typer.echo(rendered)
            typer.echo("")
            continue

        existing = out_path.read_text(encoding="utf-8") if out_path.exists() else None
        if existing and not has_marker(existing) and not force:
            skipped.append(target.filename)
            continue

        final = merge_with_existing(rendered, existing)
        out_path.write_text(final, encoding="utf-8")
        wrote += 1
        typer.echo(typer.style(f"✓ wrote {target.filename}", fg=typer.colors.GREEN))

    if skipped:
        typer.echo("")
        for name in skipped:
            typer.echo(
                typer.style(
                    f"⚠ Refusing to overwrite {name} (no changebrief marker found). "
                    "Pass --force to overwrite.",
                    fg=typer.colors.YELLOW,
                ),
                err=True,
            )

    if dry_run:
        return

    if not wrote and not skipped:
        typer.echo(typer.style("No targets selected.", fg=typer.colors.YELLOW), err=True)
        raise typer.Exit(ExitCodes.VALIDATION_ERROR)

    typer.echo("")
    typer.echo(
        typer.style(
            "Tip: re-run safely. Anything outside the changebrief markers is preserved.",
            fg=typer.colors.CYAN,
        )
    )
