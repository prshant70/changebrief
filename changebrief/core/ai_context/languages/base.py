"""Language adapter contract.

Each adapter is a small, dependency-free unit that knows three things about
its language:

1. how to *detect* its presence in a repo (so the scanner can skip it cheaply
   when it isn't there);
2. how to *gather* a :class:`LanguageProfile` (frameworks, package manager,
   test framework, scripts, source/test directories, entry points);
3. which file extensions it owns (so the scanner can attribute file counts).

Every claim emitted by an adapter MUST be backed by an :class:`Evidence`
record naming the source file. This is what prevents the generator from
shipping made-up facts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Tuple

from changebrief.core.ai_context.models import LanguageProfile


class LanguageAdapter(ABC):
    """Detect a language and gather its repo-specific signals."""

    name: str = ""
    file_extensions: Tuple[str, ...] = ()
    config_files: Tuple[str, ...] = ()

    @abstractmethod
    def detect(self, root: Path, files_by_ext: Dict[str, int]) -> bool:
        """Return ``True`` when this language is present in ``root``."""

    @abstractmethod
    def gather(self, root: Path) -> LanguageProfile:
        """Collect per-language signals for ``root``. Always evidence-backed."""

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
