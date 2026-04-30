"""Deterministic git-diff based change analysis (language agnostic)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class ChangeSummary:
    files: List[str]
    functions: List[str]
    diff_text: str


_EXCLUDED_PATHSPECS = [
    ":(exclude)*.lock",
    ":(exclude)poetry.lock",
    ":(exclude)Pipfile.lock",
    ":(exclude)package-lock.json",
    ":(exclude)pnpm-lock.yaml",
    ":(exclude)yarn.lock",
    ":(exclude)Cargo.lock",
]


def _run_git(repo: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {err}")
    return proc.stdout


def analyze_changes(base: str, feature: str, *, repo_path: Optional[str] = None) -> ChangeSummary:
    """
    Compute a best-effort summary of code changes from ``git diff base...feature``.

    - **files**: changed file paths (relative to repo root)
    - **functions**: best-effort "symbols" (language-agnostic, may be empty)
    - **diff_text**: full diff text (unified)
    """
    repo = Path(repo_path or ".").expanduser().resolve()

    # Exclude dependency lockfiles and similar noisy artifacts from diff-based planning.
    pathspec = ["--", ".", *_EXCLUDED_PATHSPECS]
    diff_text = _run_git(repo, ["diff", f"{base}...{feature}", *pathspec])
    files_raw = _run_git(repo, ["diff", "--name-only", f"{base}...{feature}", *pathspec])
    files = [line.strip() for line in files_raw.splitlines() if line.strip()]

    # v1 language-agnostic: do not try to parse language-specific symbols.
    # Leave `functions` as an empty list; downstream uses diff + tools + LLM.
    return ChangeSummary(files=files, functions=[], diff_text=diff_text)

