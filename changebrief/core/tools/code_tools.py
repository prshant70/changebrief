"""Tiny language-agnostic tool system for LLM-assisted planning/execution."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class CodeTools:
    repo_path: Path
    base: str
    feature: str
    diff_text: str
    changed_files: List[str]
    config: dict

    def search_code(self, query: str) -> List[str]:
        """
        Naive substring search returning up to ~50 file paths with matches.
        (Deterministic, avoids external deps like ripgrep for v1.)
        """
        q = (query or "").strip()
        if not q:
            return []
        hits: list[str] = []
        skip = {".git", "__pycache__", ".venv", "venv", ".tox", "node_modules", ".mypy_cache"}
        for p in self.repo_path.rglob("*"):
            if any(part in skip or part.startswith(".") for part in p.parts):
                continue
            if not p.is_file():
                continue
            if p.suffix not in {".py", ".txt", ".md", ".toml", ".yaml", ".yml", ".json"}:
                continue
            try:
                txt = p.read_text(encoding="utf-8")
            except OSError:
                continue
            if q in txt:
                hits.append(str(p.relative_to(self.repo_path)))
                if len(hits) >= 50:
                    break
        return hits

    def read_file(self, path: str) -> str:
        p = (self.repo_path / path).resolve() if not Path(path).is_absolute() else Path(path)
        try:
            return p.read_text(encoding="utf-8")
        except OSError as exc:
            return f"ERROR: could not read file: {exc}"

    def list_files(self, *, suffix: str | None = None, limit: int = 200) -> List[str]:
        """
        List repository files (relative paths). Optionally filter by file suffix.
        """
        suf = (suffix or "").strip()
        if suf and not suf.startswith("."):
            suf = "." + suf

        skip = {".git", "__pycache__", ".venv", "venv", ".tox", "node_modules", ".mypy_cache"}
        out: list[str] = []
        for p in self.repo_path.rglob("*"):
            if any(part in skip or part.startswith(".") for part in p.parts):
                continue
            if not p.is_file():
                continue
            if suf and p.suffix != suf:
                continue
            out.append(str(p.relative_to(self.repo_path)))
            if len(out) >= max(1, int(limit)):
                break
        return out

    def git_show(self, *, ref: str, path: str) -> str:
        """
        Read file content at a git ref (e.g. base branch) for context.
        """
        proc = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return f"ERROR: git show failed: {err}"
        return proc.stdout

    def get_diff(self) -> str:
        return self.diff_text

    def _run_shell(self, cmd: List[str], *, timeout_s: int = 60) -> str:
        proc = subprocess.run(
            cmd,
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return out.strip()

