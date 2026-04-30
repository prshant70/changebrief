"""Typed signals exchanged across the ai-context pipeline.

Every claim that ends up in the generated markdown carries an :class:`Evidence`
record with the file/line that supports it. The composer refuses to render
unsupported claims, which is what keeps the output specific to the repo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Evidence:
    """A single observed fact and the source that proved it."""

    fact: str
    source: str  # repo-relative path, or e.g. "git", "fs"


@dataclass
class LanguageProfile:
    """Per-language signals gathered by a :class:`LanguageAdapter`."""

    language: str
    package_manager: Optional[str] = None
    frameworks: List[Evidence] = field(default_factory=list)
    test_framework: Optional[str] = None
    run_scripts: Dict[str, str] = field(default_factory=dict)  # name -> command
    source_dirs: List[str] = field(default_factory=list)
    test_dirs: List[str] = field(default_factory=list)
    entry_points: List[str] = field(default_factory=list)
    extra_notes: List[Evidence] = field(default_factory=list)
    # Top-level packages imported across source files (package -> file count).
    # Used to surface internal/private frameworks not in the curated map.
    major_imports: Dict[str, int] = field(default_factory=dict)
    # Names declared in pyproject/package.json so we can distinguish "imported
    # third-party" from "imported internal package".
    declared_dependencies: List[str] = field(default_factory=list)


@dataclass
class RepoContext:
    """Repo-wide context derived by :func:`scan_repo`."""

    root: str
    project_name: Optional[str]
    project_summary: Optional[str]
    primary_language: Optional[str]
    languages_by_files: Dict[str, int]  # language -> file count
    profiles: List[LanguageProfile]
    top_directories: List["DirectoryRole"]
    nested_directories: List["DirectoryRole"]  # depth-2 dirs (parent_dir set)
    file_naming_patterns: List[Evidence]
    has_readme: bool
    has_contributing: bool
    has_security: bool
    has_codeowners: bool
    has_editorconfig: bool
    has_precommit: bool
    has_ci: List[str]
    license_name: Optional[str]
    git_branch: Optional[str]
    repo_size_files: int


@dataclass(frozen=True)
class DirectoryRole:
    """One directory and its inferred role.

    ``parent_dir`` is ``None`` for top-level dirs and the parent's *name* for
    nested ones (e.g. ``parent_dir="app"`` for ``app/services``).
    """

    name: str  # the directory's own name (e.g. "services" for app/services)
    role: str  # e.g. "source", "tests", "business", "data_access", ...
    description: str
    parent_dir: Optional[str] = None
    rel_path: Optional[str] = None  # repo-relative path, e.g. "app/services"


@dataclass
class ContextConfig:
    """Optional org/repo overrides loaded from .changebrief/context.yaml."""

    project_summary: Optional[str] = None
    do: List[str] = field(default_factory=list)
    dont: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    # Map from package name to a friendly description. Lets teams teach the
    # tool about private/internal frameworks (e.g. "torpedo": "Torpedo —
    # 1mg's Sanic-based async framework").
    frameworks: Dict[str, str] = field(default_factory=dict)


@dataclass
class AIContextSection:
    title: str
    bullets: List[str] = field(default_factory=list)
    paragraphs: List[str] = field(default_factory=list)
    omit_if_empty: bool = True


@dataclass
class AIContext:
    """The composed, agent-ready context — fed to :func:`render`."""

    project_name: str
    overview: str
    sections: List[AIContextSection]
