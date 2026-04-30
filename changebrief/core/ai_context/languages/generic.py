"""Catch-all adapter so the tool produces something useful for any repo."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict

from changebrief.core.ai_context.languages.base import LanguageAdapter
from changebrief.core.ai_context.models import LanguageProfile


class GenericAdapter(LanguageAdapter):
    """Always detects (last in registry) so the scanner always returns *some* profile."""

    name = "generic"
    file_extensions = ()
    config_files = ("Makefile", "justfile")

    def detect(self, root: Path, files_by_ext: Dict[str, int]) -> bool:
        return True

    def gather(self, root: Path) -> LanguageProfile:
        profile = LanguageProfile(language=self.name)
        # Promote Makefile/justfile targets as run scripts when nothing else found them.
        mk = root / "Makefile"
        if mk.exists():
            for line in self._read(mk).splitlines():
                if line and not line.startswith((" ", "\t", "#", ".")) and ":" in line:
                    target = line.split(":", 1)[0].strip()
                    if target.replace("-", "").replace("_", "").isalnum():
                        profile.run_scripts[target] = f"make {target}"
        jf = root / "justfile"
        if jf.exists():
            for line in self._read(jf).splitlines():
                stripped = line.strip()
                if (
                    stripped
                    and not stripped.startswith(("#", "@", "_", "set "))
                    and ":" in stripped
                    and not stripped.startswith(("export ",))
                ):
                    target = stripped.split(":", 1)[0].strip()
                    if target and target.replace("-", "").replace("_", "").isalnum():
                        profile.run_scripts[target] = f"just {target}"

        # README command fences: best-effort extraction of install/run/test commands.
        for name in ("README.md", "README.rst", "README.txt", "README"):
            rp = root / name
            if not rp.exists():
                continue
            try:
                text = self._read(rp)
            except Exception:
                text = ""
            if not text:
                break
            for key, cmd in _readme_scripts(text):
                profile.run_scripts.setdefault(key, cmd)
            break
        return profile


_FENCE_RE = re.compile(r"```(?:bash|sh|shell|zsh|console|text)?\s*\n([\s\S]*?)\n```", re.IGNORECASE)


def _readme_scripts(text: str) -> list[tuple[str, str]]:
    """Extract a small set of canonical scripts from README fenced blocks."""
    scripts: list[tuple[str, str]] = []

    def add(name: str, cmd: str) -> None:
        if not cmd.strip():
            return
        # Deduplicate by command string.
        if any(c == cmd for _, c in scripts):
            return
        scripts.append((name, cmd.strip()))

    for m in _FENCE_RE.finditer(text or ""):
        block = m.group(1) or ""
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Skip obvious non-commands.
            if line.startswith(("git clone", "cd ")):
                continue
            if re.match(r"^(pip|pip3)\s+install\s+-r\s+.+", line):
                add("install", line)
            elif re.match(r"^(python|python3)\s+-m\s+[\w.]+", line):
                add("run", line)
            elif re.match(r"^pytest(\s|$)", line):
                add("tests", line)
            elif re.match(r"^make\s+\w[\w\-]*", line):
                add("make", line)

        if len(scripts) >= 6:
            break

    return scripts
