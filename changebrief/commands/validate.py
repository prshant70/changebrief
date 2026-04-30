"""changebrief validate — change-aware validation assistant."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from changebrief.core.analyzer.change_analyzer import analyze_changes
from changebrief.core.context import require_app_context
from changebrief.core.error_handler import handle_errors
from changebrief.core.exit_codes import ExitCodes
from changebrief.core.llm.validation_planner import (
    SYSTEM_PROMPT,
    VALIDATION_PLAN_SCHEMA,
    render_json,
    render_markdown,
    render_pretty,
)
from changebrief.core.models.requests import ValidateRequest
from changebrief.core.redaction import redact_with_counts
from changebrief.core.services import ValidationService
from changebrief.core.validator import validate_git_branch, validate_path_exists

validate_app = typer.Typer(
    help="Validate regressions between two branches.",
    epilog="Example:\n  changebrief validate --base main --feature feature/payments",
    invoke_without_command=True,
)


_FAIL_ON_CHOICES = ("never", "low", "medium", "high")
_FORMAT_CHOICES = ("pretty", "json", "markdown")
_RISK_RANK = {"low": 1, "medium": 2, "high": 3}


def _validate_choice(value: str, *, choices: tuple[str, ...], flag: str) -> str:
    norm = (value or "").strip().lower()
    if norm not in choices:
        raise typer.BadParameter(
            f"--{flag} must be one of: {', '.join(choices)}; got {value!r}.",
        )
    return norm


def _gate_failed(merge_risk: str, fail_on: str) -> bool:
    if fail_on == "never":
        return False
    return _RISK_RANK.get(merge_risk, 0) >= _RISK_RANK.get(fail_on, 99)


@validate_app.callback()
@handle_errors
def validate(
    ctx: typer.Context,
    base: str = typer.Option(..., "--base", help="Base branch or ref."),
    feature: str = typer.Option(
        ...,
        "--feature",
        help="Feature branch or ref to validate against the base.",
    ),
    nocache: bool = typer.Option(
        False,
        "--nocache",
        help="Bypass the local cache and run the full pipeline.",
    ),
    path: Optional[Path] = typer.Option(
        None,
        "--path",
        "-p",
        help="Git repository root (defaults to current directory).",
        file_okay=False,
        dir_okay=True,
    ),
    fail_on: str = typer.Option(
        "never",
        "--fail-on",
        help="Exit non-zero when merge_risk is at or above this level: never|low|medium|high.",
    ),
    output_format: str = typer.Option(
        "pretty",
        "--format",
        help="Output format: pretty|json|markdown.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be sent to the LLM (after redaction) and exit without API calls.",
    ),
) -> None:
    """Check for regressions between branches.

    Exit codes:
      0  — success (or merge_risk is below the --fail-on threshold)
      30 — merge-risk gate tripped (see --fail-on)
      2  — validation error (e.g. not a git repo, invalid ref)
      3  — config error
    """
    fail_on_norm = _validate_choice(fail_on, choices=_FAIL_ON_CHOICES, flag="fail-on")
    format_norm = _validate_choice(output_format, choices=_FORMAT_CHOICES, flag="format")

    app_ctx = require_app_context(ctx)

    if dry_run:
        _dry_run(base=base, feature=feature, path=path)
        return

    request = ValidateRequest(
        base=base,
        feature=feature,
        path=str(path) if path else None,
        nocache=nocache,
    )
    result = ValidationService(app_ctx).run(request)

    if format_norm == "pretty":
        typer.echo("🔍 Analyzing changes...\n")
        typer.echo(render_pretty(result.plan, intent=result.intent, confidence=result.confidence))
    elif format_norm == "markdown":
        typer.echo(render_markdown(result.plan, intent=result.intent, confidence=result.confidence))
    else:  # json
        typer.echo(render_json(result.plan, intent=result.intent, confidence=result.confidence))

    if _gate_failed(result.plan.merge_risk, fail_on_norm):
        typer.secho(
            f"Merge-risk gate tripped: merge_risk={result.plan.merge_risk.upper()} "
            f">= --fail-on={fail_on_norm.upper()}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(ExitCodes.MERGE_RISK_GATE)


def _dry_run(*, base: str, feature: str, path: Optional[Path]) -> None:
    """Show exactly what would leave the machine, then exit 0."""
    repo = validate_path_exists(path, kind="Repository path") if path else Path(".").resolve()
    base_resolved = validate_git_branch(base, repo=repo)
    feature_resolved = validate_git_branch(feature, repo=repo)

    summary = analyze_changes(base_resolved, feature_resolved, repo_path=str(repo))
    redacted_diff, counts = redact_with_counts(summary.diff_text)

    typer.echo("📝 ChangeBrief dry-run — nothing will be sent to the LLM.\n")
    typer.echo(f"Repository: {repo}")
    typer.echo(f"Base ref:    {base_resolved}")
    typer.echo(f"Feature ref: {feature_resolved}")
    typer.echo(f"Changed files: {len(summary.files)}")
    typer.echo(f"Diff size (raw / redacted): {len(summary.diff_text)} / {len(redacted_diff)} bytes")
    if counts:
        typer.echo("\nRedactions applied:")
        for kind, n in sorted(counts.items()):
            typer.echo(f"  - {kind}: {n}")
    else:
        typer.echo("\nRedactions applied: (none)")

    typer.echo("\nLLM contract that *would* be used:")
    typer.echo(f"  schema: {VALIDATION_PLAN_SCHEMA['name']} (strict={VALIDATION_PLAN_SCHEMA.get('strict')})")
    typer.echo(f"  system prompt bytes: {len(SYSTEM_PROMPT)}")

    preview_n = 1500
    if redacted_diff:
        typer.echo("\nRedacted diff preview (first 1500 chars):")
        typer.echo("---")
        typer.echo(redacted_diff[:preview_n])
        if len(redacted_diff) > preview_n:
            typer.echo("... [truncated]")
        typer.echo("---")
