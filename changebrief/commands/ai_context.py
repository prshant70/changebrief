"""changebrief ai-context — generate per-agent context files."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import typer

from changebrief.core.ai_context.builder import (
    build_framework_entry,
    preview_merge,
    upsert_framework_entry,
)
from changebrief.core.ai_context.composer import compose_context
from changebrief.core.ai_context.config_loader import load_context_config
from changebrief.core.ai_context.dependency_learner import enrich_config_from_dependencies
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
from changebrief.core.exceptions import ValidationError
from changebrief.core.exit_codes import ExitCodes
from changebrief.utils.paths import get_config_dir


ai_context_app = typer.Typer(
    help="Generate per-agent context files (CLAUDE.md, CURSOR.md, CODEX.md).",
    epilog=(
        "Examples:\n"
        "  changebrief ai-context init\n"
        "  changebrief ai-context init --path /path/to/repo --dry-run\n"
        "  changebrief ai-context init --targets claude --targets cursor\n"
        "  changebrief ai-context build --path /path/to/custom-framework"
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
    enrich_deps: bool = typer.Option(
        False,
        "--enrich-deps",
        help=(
            "When enabled, discover pinned git dependencies (Python/Node v1), check them "
            "out into a local cache, build framework context for each dependency, and "
            "merge that into this run's context so generated files are framework-aware."
        ),
    ),
    dep_hosts: List[str] = typer.Option(
        ["github.com", "bitbucket.org", "file"],
        "--dep-hosts",
        help=(
            "Allowed dependency hosts for `--enrich-deps` (repeatable). "
            "Defaults to github.com, bitbucket.org, and file:// (for local deps/tests)."
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

    if enrich_deps:
        dep_cfg, learned, skipped = enrich_config_from_dependencies(
            repo_path,
            config=app_ctx.config,
            allow_hosts=dep_hosts,
            llm_enabled=enrich_with_llm,
        )
        # Merge learned dependency config into the effective config (but do not
        # override explicit user config keys).
        if dep_cfg.frameworks:
            for k, v in dep_cfg.frameworks.items():
                if k not in cfg.frameworks:
                    cfg.frameworks[k] = v
        for item in dep_cfg.do:
            if item and item not in cfg.do:
                cfg.do.append(item)
        for item in dep_cfg.dont:
            if item and item not in cfg.dont:
                cfg.dont.append(item)
        for item in dep_cfg.notes:
            if item and item not in cfg.notes:
                cfg.notes.append(item)

        if learned:
            typer.echo(
                typer.style(
                    "  learned dependency frameworks: "
                    + ", ".join(f"{d.package_name}@{d.ref}" for d in learned[:6])
                    + ("…" if len(learned) > 6 else ""),
                    fg=typer.colors.CYAN,
                )
            )
        if skipped and not learned:
            typer.echo(
                typer.style(
                    "  dependency learning skipped: " + "; ".join(skipped[:3]),
                    fg=typer.colors.YELLOW,
                ),
                err=True,
            )
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


@ai_context_app.command("build")
@handle_errors
def build_cmd(
    ctx: typer.Context,
    path: Path = typer.Option(
        Path("."),
        "--path",
        "-p",
        help="Path to the repo / custom-framework / utility to learn about.",
        file_okay=False,
        dir_okay=True,
    ),
    name: Optional[str] = typer.Option(
        None,
        "--name",
        "-n",
        help=(
            "Override the detected import name (e.g. `torpedo`). Defaults to the "
            "first top-level Python package or the project name."
        ),
    ),
    description: Optional[str] = typer.Option(
        None,
        "--description",
        "-d",
        help=(
            "Override the framework description stored in context.yaml. "
            "Wins over both the LLM and the deterministic fallback."
        ),
    ),
    note: List[str] = typer.Option(
        [],
        "--note",
        help=(
            "Add an extra usage note (repeatable). Appended to whatever the "
            "extractor and LLM produced under `notes:`."
        ),
    ),
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help=(
            "Path to the context config to update "
            "(defaults to ~/.changebrief/context.yaml)."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Replace an existing entry for this framework.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the YAML that would be written and exit without touching disk.",
    ),
    enrich: bool = typer.Option(
        True,
        "--enrich/--no-enrich",
        help=(
            "Use the configured LLM to synthesise rich `do` / `dont` / `notes` "
            "with verified citations. On by default; ``--no-enrich`` produces a "
            "deterministic-only entry from public-API and exception signals."
        ),
    ),
) -> None:
    """
    Scan a repo / custom framework / utility and write a rich entry to context.yaml.

    The pipeline runs an AST-driven deterministic extractor (public API,
    exception family, notable directories, examples, docs, config keys,
    Python version pin) and — when an LLM is available — an optional
    synthesis pass that produces idiomatic ``do`` / ``dont`` / ``notes``
    bullets with file citations verified against the framework's source.
    The resulting entry is consumed by ``changebrief ai-context init`` in
    every repo that imports this framework.
    """
    app_ctx = require_app_context(ctx)
    target_path = path.resolve()
    if not target_path.is_dir():
        raise ValidationError(f"--path must be an existing directory: {target_path}")

    repo_ctx = scan_repo(target_path)
    try:
        report = build_framework_entry(
            repo_ctx,
            name_override=name,
            description_override=description,
            user_notes=note,
            config=app_ctx.config,
            llm_enabled=enrich,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    entry = report.entry
    extraction = report.extraction
    synthesis = report.synthesis

    config_path = (
        Path(config).expanduser().resolve()
        if config is not None
        else get_config_dir() / "context.yaml"
    )

    typer.echo(typer.style(f"Scanned {target_path}", bold=True))
    typer.echo(f"  framework:        {entry.name}")
    if extraction.package_dir:
        typer.echo(f"  package source:   {extraction.package_dir}/")
    if extraction.public_api:
        typer.echo(
            f"  public API:       {len(extraction.public_api)} symbol(s) "
            f"({', '.join(s.name for s in extraction.public_api[:6])}"
            + ("…" if len(extraction.public_api) > 6 else "")
            + ")"
        )
    if extraction.exceptions:
        typer.echo(f"  exceptions:       {len(extraction.exceptions)} class(es)")
    if extraction.notable_dirs:
        typer.echo(
            f"  notable dirs:     "
            + ", ".join(nd.name for nd in extraction.notable_dirs[:6])
        )
    if extraction.python_version_pin:
        typer.echo(f"  python pin:       {extraction.python_version_pin}")
    if synthesis.used_llm:
        typer.echo(
            typer.style(
                f"  enriched via LLM"
                + (" (cache hit)" if synthesis.cache_hit else "")
                + f" [{synthesis.model_used}]: "
                + f"{len(entry.do)} do, {len(entry.dont)} dont, "
                + f"{len(synthesis.notes)} llm-notes, "
                + f"{len(entry.related_frameworks)} related"
                + (
                    f" ({synthesis.items_dropped} dropped — bad citations)"
                    if synthesis.items_dropped
                    else ""
                ),
                fg=typer.colors.CYAN,
            )
        )
    elif enrich:
        typer.echo(
            typer.style(
                f"  enrichment skipped: {synthesis.reason_skipped}",
                fg=typer.colors.YELLOW,
            )
        )
    typer.echo(f"  target file:      {config_path}")
    typer.echo("")

    if dry_run:
        typer.echo(typer.style("--- proposed context.yaml (dry-run) ---", bold=True))
        typer.echo(preview_merge(entry, config_path))
        return

    conflict, written_path = upsert_framework_entry(entry, config_path, force=force)
    if conflict:
        typer.echo(
            typer.style(
                f"⚠ Refusing to overwrite existing entry for `{entry.name}` in "
                f"{written_path}. Pass --force to replace.",
                fg=typer.colors.YELLOW,
            ),
            err=True,
        )
        raise typer.Exit(ExitCodes.VALIDATION_ERROR)

    typer.echo(
        typer.style(
            f"✓ added `{entry.name}` to {written_path}",
            fg=typer.colors.GREEN,
        )
    )
    typer.echo(
        typer.style(
            "Tip: re-run `changebrief ai-context init` in any repo that imports "
            f"`{entry.name}` to surface the new description.",
            fg=typer.colors.CYAN,
        )
    )
