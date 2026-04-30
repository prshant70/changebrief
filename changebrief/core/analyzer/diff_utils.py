"""Shared diff parsing helpers used by the heuristic analyzers.

The heuristics in :mod:`changebrief.core.analyzer.{risk,intent,confidence}_classifier`
need three things that the raw diff text cannot provide directly:

1. The **added** and **removed** content lines, separated from diff metadata
   (``diff --git`` headers, ``@@`` hunks, ``+++ b/path`` paths, etc.).
2. The **source file** each line came from, so a Python pattern is only matched
   against ``.py`` lines and a TypeScript pattern only against ``.ts/.tsx``.
3. Lightweight bucket labels for **test**, **config**, and **migration** files
   so we can apply file-level heuristics that don't depend on per-line content.

Keeping these helpers here means analyzers don't reinvent fragile regex over
the full lowercased diff and don't accidentally trigger on path/comment text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Iterator, List


@dataclass(frozen=True)
class DiffLine:
    """A single added or removed source line with the file it came from."""

    file: str
    text: str


_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$")


def _iter_lines(diff_text: str, kind: str) -> Iterator[DiffLine]:
    """Yield added (``kind='+'``) or removed (``kind='-'``) lines, paired with file path.

    The parser tolerates the simplified diff format used in tests (where the
    ``diff --git`` header may be absent and the file is announced only via
    ``+++ b/path``) as well as the full ``git diff`` output.
    """
    current_file = ""
    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git"):
            m = _DIFF_HEADER.match(line)
            if m:
                current_file = m.group("b")
            continue
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path and path != "/dev/null":
                current_file = path
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("@@"):
            continue
        if line.startswith("\\"):
            # e.g. "\ No newline at end of file"
            continue
        if kind == "+" and line.startswith("+") and not line.startswith("++"):
            yield DiffLine(file=current_file, text=line[1:])
        elif kind == "-" and line.startswith("-") and not line.startswith("--"):
            yield DiffLine(file=current_file, text=line[1:])


def iter_added_lines(diff_text: str) -> Iterator[DiffLine]:
    return _iter_lines(diff_text, "+")


def iter_removed_lines(diff_text: str) -> Iterator[DiffLine]:
    return _iter_lines(diff_text, "-")


# ---------------------------------------------------------------------------
# Language and file-bucket detection.
# ---------------------------------------------------------------------------

_LANG_BY_EXT = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".scala": "scala",
    ".swift": "swift",
    ".sql": "sql",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".ini": "ini",
    ".env": "env",
    ".md": "markdown",
    ".rst": "markdown",
    ".html": "html",
    ".css": "css",
    ".scss": "css",
}


def language_of(path: str) -> str:
    """Best-effort language label for a path (``"other"`` if unknown)."""
    p = (path or "").lower()
    for ext, lang in _LANG_BY_EXT.items():
        if p.endswith(ext):
            return lang
    return "other"


_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:test_[^/]+|[^/]+_test\.[^./]+|tests?/|spec/|specs/|__tests__/)",
    re.IGNORECASE,
)


def is_test_file(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path or ""))


_CONFIG_BASENAMES = {
    ".env",
    ".env.local",
    "config.yaml",
    "config.yml",
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
    "dockerfile",
    "values.yaml",
}

_CONFIG_EXTENSIONS = {".env", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg"}


def is_config_file(path: str) -> bool:
    p = path or ""
    if not p or is_test_file(p):
        return False
    base = p.rsplit("/", 1)[-1].lower()
    if base in _CONFIG_BASENAMES:
        return True
    return any(base.endswith(ext) for ext in _CONFIG_EXTENSIONS)


def is_migration_file(path: str) -> bool:
    p = (path or "").lower()
    if not p:
        return False
    if p.endswith(".sql"):
        return True
    return "migration" in p or "/migrate/" in p


def is_source_language(lang: str) -> bool:
    """Heuristic: is the language one we want to scan for code-level patterns?"""
    return lang in {
        "python",
        "javascript",
        "typescript",
        "go",
        "rust",
        "java",
        "kotlin",
        "ruby",
        "php",
        "csharp",
        "cpp",
        "c",
        "scala",
        "swift",
        "sql",
    }


def filter_source_lines(lines: Iterable[DiffLine]) -> List[DiffLine]:
    """Keep lines whose file looks like source code we can pattern-match on."""
    return [ln for ln in lines if is_source_language(language_of(ln.file))]
