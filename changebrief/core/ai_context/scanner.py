"""Repository scanner — extracts evidence-backed signals about a project."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from changebrief.core.ai_context.conventions import (
    detect_ci_providers,
    detect_license,
    file_count_by_extension,
    file_naming_patterns,
    list_nested_directories,
    list_top_directories,
)
from changebrief.core.ai_context.languages.registry import get_adapters
from changebrief.core.ai_context.models import LanguageProfile, RepoContext


# Cap so we don't walk monorepos for hours.
_IMPORT_SCAN_FILE_CAP = 800
_IMPORT_RE = re.compile(r"^(?:from\s+([\w.]+)|import\s+([\w.]+))", re.MULTILINE)


def _python_stdlib_names() -> set[str]:
    """Best-effort stdlib name set; ``sys.stdlib_module_names`` is 3.10+."""
    import sys as _sys

    names = set(getattr(_sys, "stdlib_module_names", set()))
    # A few extras agents commonly hit.
    names.update({"__future__", "typing_extensions"})
    return names


def scan_repo(path: str | Path) -> RepoContext:
    """Walk ``path`` and return a fully-populated :class:`RepoContext`.

    Cheap, deterministic, side-effect free. Skips vendored/build/cache dirs
    so the scan stays fast on large monorepos.
    """
    root = Path(path).resolve()
    if not root.exists():
        raise FileNotFoundError(root)

    files_by_ext, total_files = file_count_by_extension(root)
    profiles = _gather_language_profiles(root, files_by_ext)
    primary = _pick_primary_language(profiles, files_by_ext)

    top_dirs = list_top_directories(root)
    nested_dirs = list_nested_directories(root, top_dirs)

    source_dirs: list[str] = []
    for p in profiles:
        for d in p.source_dirs:
            if d not in source_dirs:
                source_dirs.append(d)
    naming_patterns = file_naming_patterns(root, source_dirs=source_dirs)

    # Populate `major_imports` for the Python profile by scanning source files
    # for top-level imports. This is what surfaces internal/private frameworks
    # (e.g. Torpedo) that aren't in the curated map.
    py_profile = next((p for p in profiles if p.language == "python"), None)
    if py_profile is not None:
        py_profile.major_imports = _scan_python_imports(root, py_profile.source_dirs)

    name, summary = _project_name_and_summary(root, profiles)

    return RepoContext(
        root=str(root),
        project_name=name,
        project_summary=summary,
        primary_language=primary,
        languages_by_files={
            adapter_name: count
            for adapter_name, count in _languages_by_files(profiles, files_by_ext).items()
        },
        profiles=profiles,
        top_directories=top_dirs,
        nested_directories=nested_dirs,
        file_naming_patterns=naming_patterns,
        has_readme=_exists_any(root, ("README.md", "README.rst", "README.txt", "README")),
        has_contributing=_exists_any(root, ("CONTRIBUTING.md", "CONTRIBUTING.rst", "CONTRIBUTING")),
        has_security=_exists_any(root, ("SECURITY.md", "SECURITY")),
        has_codeowners=_exists_any(root, ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")),
        has_editorconfig=(root / ".editorconfig").exists(),
        has_precommit=(root / ".pre-commit-config.yaml").exists(),
        has_ci=detect_ci_providers(root),
        license_name=detect_license(root),
        git_branch=_git_branch(root),
        repo_size_files=total_files,
    )


# ---------------------------------------------------------------------------- helpers


def _gather_language_profiles(
    root: Path, files_by_ext: Dict[str, int]
) -> List[LanguageProfile]:
    profiles: List[LanguageProfile] = []
    for adapter in get_adapters():
        try:
            if not adapter.detect(root, files_by_ext):
                continue
        except Exception:  # pragma: no cover — defensive: a flaky adapter shouldn't kill the scan
            continue
        try:
            profile = adapter.gather(root)
        except Exception:  # pragma: no cover — same
            continue
        if profile.language == "generic" and profiles:
            # Skip the generic adapter when a specific one already handled the repo,
            # unless it found scripts that the specific adapter didn't.
            if not profile.run_scripts:
                continue
        profiles.append(profile)
    return profiles


def _languages_by_files(
    profiles: List[LanguageProfile], files_by_ext: Dict[str, int]
) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for profile in profiles:
        if profile.language == "generic":
            continue
        adapter = next((a for a in get_adapters() if a.name == profile.language), None)
        if not adapter:
            continue
        counts[profile.language] = sum(
            files_by_ext.get(ext, 0) for ext in adapter.file_extensions
        )
    return counts


def _pick_primary_language(
    profiles: List[LanguageProfile], files_by_ext: Dict[str, int]
) -> Optional[str]:
    counts = _languages_by_files(profiles, files_by_ext)
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _exists_any(root: Path, names: tuple[str, ...]) -> bool:
    return any((root / n).exists() for n in names)


def _scan_python_imports(root: Path, source_dirs: list[str]) -> Dict[str, int]:
    """Return ``{top_level_package -> distinct_file_count}`` for source dirs.

    Filters stdlib, the repo's own internal packages, and relative imports.
    Capped at ``_IMPORT_SCAN_FILE_CAP`` files so monorepos stay snappy.
    """
    stdlib = _python_stdlib_names()
    internal = {d.split("/")[0] for d in source_dirs}
    counts: Dict[str, int] = {}
    files_seen = 0

    scan_roots = [root / d for d in source_dirs if (root / d).is_dir()] or [root]
    for sr in scan_roots:
        for path in sr.rglob("*.py"):
            if files_seen >= _IMPORT_SCAN_FILE_CAP:
                return counts
            if any(
                part in {".venv", "venv", "__pycache__", ".tox", ".mypy_cache"}
                for part in path.parts
            ):
                continue
            files_seen += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            seen_in_file: set[str] = set()
            for m in _IMPORT_RE.finditer(text):
                qualified = (m.group(1) or m.group(2) or "").strip()
                if not qualified:
                    continue
                top = qualified.split(".", 1)[0]
                if not top or top in stdlib or top in internal:
                    continue
                if top.startswith("_"):
                    continue
                seen_in_file.add(top)
            for top in seen_in_file:
                counts[top] = counts.get(top, 0) + 1
    return counts


def _git_branch(root: Path) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    return branch or None


def _project_name_and_summary(
    root: Path, profiles: List[LanguageProfile]
) -> tuple[Optional[str], Optional[str]]:
    """Best-effort: read pyproject/package.json, otherwise use directory name."""
    name: Optional[str] = None
    summary: Optional[str] = None

    pp = root / "pyproject.toml"
    if pp.exists():
        try:
            text = pp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        m = re.search(r"^\s*name\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
        if m:
            name = m.group(1)
        m = re.search(r"^\s*description\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
        if m:
            summary = m.group(1)

    if name is None:
        pkg = root / "package.json"
        if pkg.exists():
            try:
                import json

                data = json.loads(pkg.read_text(encoding="utf-8", errors="replace") or "{}")
                name = data.get("name")
                summary = summary or data.get("description")
            except (OSError, ValueError):
                pass

    if name is None:
        name = root.name
    return name, summary
