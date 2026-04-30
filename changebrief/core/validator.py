"""Reusable validation helpers for CLI inputs."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from changebrief.core.exceptions import (
    BranchValidationError,
    ConfigNotFoundError,
    PathValidationError,
)
from changebrief.utils.paths import get_config_file


def validate_path_exists(path: str | Path, *, kind: str = "Path") -> Path:
    """Ensure ``path`` exists on disk."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise PathValidationError(f"{kind} does not exist: {p}")
    return p


def validate_git_branch(branch: str, *, repo: Optional[Path] = None) -> str:
    """
    Ensure ``branch`` resolves to a valid git ref in ``repo`` (cwd if omitted).
    """
    name = branch.strip()
    if not name:
        raise BranchValidationError("Branch name cannot be empty.")
    cwd = Path(repo).resolve() if repo else None
    try:
        wt = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise BranchValidationError(
            "git executable not found; install Git or use a valid environment.",
        ) from exc
    if wt.returncode != 0 or wt.stdout.strip() != "true":
        raise BranchValidationError("Not inside a git repository.")
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", name],
            check=True,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
    except subprocess.CalledProcessError as exc:
        raise BranchValidationError(
            f"Not a valid git ref in this repository: {name!r}",
        ) from exc
    return name


def resolve_git_sha(ref: str, *, repo: Optional[Path] = None) -> str:
    """Resolve a ref to a commit SHA in ``repo`` (cwd if omitted)."""
    cwd = Path(repo).resolve() if repo else None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", ref],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise BranchValidationError("git executable not found; install Git.") from exc
    if proc.returncode != 0:
        raise BranchValidationError(f"Not a valid git ref in this repository: {ref!r}")
    return proc.stdout.strip()


def validate_config_present(path: Optional[Path] = None) -> Path:
    """Ensure the default (or explicit) config file exists."""
    p = path.expanduser().resolve() if path else get_config_file()
    if not p.is_file():
        raise ConfigNotFoundError(
            f"Config not found at {p}. Run `changebrief init` to create it.",
        )
    return p
