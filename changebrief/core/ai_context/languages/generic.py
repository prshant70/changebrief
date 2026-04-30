"""Catch-all adapter so the tool produces something useful for any repo."""

from __future__ import annotations

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
        return profile
