"""Repo-wide convention detection (independent of language)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from changebrief.core.ai_context.models import DirectoryRole, Evidence


# Directories the scanner should never enter (perf + noise).
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "bower_components",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
        "out",
        "bin",
        "obj",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".idea",
        ".vscode",
        ".cache",
        ".cargo",
        ".gradle",
        ".terraform",
        "vendor",
    }
)


# (directory_name, role, description) inferred without reading file contents.
# Roles are used both for top-level grouping and for architectural layer
# inference (see ``ARCHITECTURE_LAYERS``).
_DIR_ROLE_HINTS: Tuple[Tuple[str, str, str], ...] = (
    # source / package roots ------------------------------------------------
    ("src", "source", "Primary application source code."),
    ("lib", "source", "Library / shared modules."),
    ("app", "source", "Application source code."),
    ("apps", "source", "Multi-app source layout (one app per subfolder)."),
    ("server", "source", "Server-side code."),
    ("backend", "source", "Backend service code."),
    ("api", "entry", "API surface (handlers / route definitions)."),
    ("cmd", "source", "Go-style entry-point binaries (one folder per binary)."),
    ("internal", "source", "Go-style internal-only packages."),
    ("pkg", "source", "Go-style public packages."),
    # entry layer -----------------------------------------------------------
    ("routes", "entry", "HTTP route definitions."),
    ("controllers", "entry", "Controller layer (MVC entry points)."),
    ("handlers", "entry", "Request handlers."),
    ("views", "entry", "View layer (Django/MVC)."),
    ("endpoints", "entry", "Endpoint definitions."),
    # validation / schemas --------------------------------------------------
    ("schemas", "validation", "Request / response schemas (e.g. Pydantic)."),
    ("serializers", "validation", "Serializers (DRF-style)."),
    ("validators", "validation", "Input validators."),
    ("dto", "validation", "Data transfer objects."),
    ("dtos", "validation", "Data transfer objects."),
    # business orchestration ------------------------------------------------
    ("managers", "business", "Business orchestration layer (called from entry)."),
    ("services", "business", "Service / business-logic layer."),
    ("usecases", "business", "Use-case layer (clean architecture)."),
    ("interactors", "business", "Interactor layer (clean architecture)."),
    # data access -----------------------------------------------------------
    ("repositories", "data_access", "Data-access / repository layer."),
    ("repos", "data_access", "Data-access / repository layer."),
    ("dao", "data_access", "Data-access objects."),
    # external calls --------------------------------------------------------
    ("clients", "external", "Outbound clients for external services."),
    ("adapters", "external", "Adapters for external systems."),
    ("gateways", "external", "Outbound gateways."),
    ("integrations", "external", "Third-party integration code."),
    # persistence / domain models ------------------------------------------
    ("models", "persistence", "Domain models / ORM models."),
    ("entities", "persistence", "Domain entities."),
    ("db", "persistence", "Database layer."),
    ("database", "persistence", "Database layer."),
    # async / messaging ----------------------------------------------------
    ("events", "messaging", "Event publishers / subscribers."),
    ("tasks", "messaging", "Background tasks (e.g. Celery)."),
    ("workers", "messaging", "Long-running workers."),
    ("jobs", "messaging", "Scheduled jobs."),
    ("consumers", "messaging", "Message consumers."),
    ("producers", "messaging", "Message producers."),
    # cross-cutting ---------------------------------------------------------
    ("middlewares", "cross_cutting", "HTTP / pipeline middleware."),
    ("middleware", "cross_cutting", "HTTP / pipeline middleware."),
    ("decorators", "cross_cutting", "Reusable function decorators."),
    ("exceptions", "cross_cutting", "Custom exception types."),
    ("errors", "cross_cutting", "Custom error types."),
    ("constants", "cross_cutting", "Project-wide constants."),
    ("enums", "cross_cutting", "Enumerations."),
    ("utils", "cross_cutting", "Utility helpers."),
    ("util", "cross_cutting", "Utility helpers."),
    ("helpers", "cross_cutting", "Helpers."),
    ("common", "cross_cutting", "Shared / common modules."),
    ("shared", "cross_cutting", "Shared / common modules."),
    ("types", "cross_cutting", "Shared type definitions."),
    # tests -----------------------------------------------------------------
    ("tests", "tests", "Test suite."),
    ("test", "tests", "Test suite."),
    ("__tests__", "tests", "Test suite (Jest/Vitest convention)."),
    ("spec", "tests", "RSpec-style test suite."),
    ("specs", "tests", "Spec-style test suite."),
    # docs / examples -------------------------------------------------------
    ("docs", "docs", "Project documentation."),
    ("doc", "docs", "Project documentation."),
    ("examples", "docs", "Usage examples."),
    # ops / infra -----------------------------------------------------------
    ("scripts", "ops", "Operational scripts (build, deploy, dev tooling)."),
    ("bin", "ops", "Executable scripts."),
    ("ops", "ops", "Operational tooling."),
    ("infra", "infra", "Infrastructure-as-code."),
    ("deploy", "infra", "Deployment manifests."),
    ("terraform", "infra", "Terraform IaC."),
    ("k8s", "infra", "Kubernetes manifests."),
    ("helm", "infra", "Helm charts."),
    # data / migrations -----------------------------------------------------
    ("migrations", "data", "Database migrations."),
    ("migrate", "data", "Database migration scripts."),
    # frontend / static -----------------------------------------------------
    ("frontend", "frontend", "Frontend application."),
    ("client", "frontend", "Client-side application."),
    ("web", "frontend", "Web frontend."),
    ("ui", "frontend", "User-interface code."),
    ("pages", "frontend", "Routed pages (Next.js/Nuxt)."),
    ("components", "frontend", "Reusable UI components."),
    ("public", "static", "Static assets served as-is."),
    ("static", "static", "Static assets."),
    ("assets", "static", "Static assets."),
    # config / ci -----------------------------------------------------------
    ("config", "config", "Project configuration."),
    ("configs", "config", "Project configuration."),
    ("workflows", "ci", "CI/CD workflow definitions."),
    (".github", "ci", "GitHub-specific config (workflows, templates)."),
    (".gitlab", "ci", "GitLab-specific config (CI, templates)."),
)


# Architectural layers, in canonical order from "outermost" to "innermost".
# Used by the composer to render an "Architecture (inferred from layout)"
# block and a typical request shape (linear arrow chain).
ARCHITECTURE_LAYERS: Tuple[Tuple[str, str, bool], ...] = (
    # (layer_id, label, include_in_request_shape)
    ("entry", "Entry layer", True),
    ("validation", "Validation / schemas", False),
    ("business", "Business orchestration", True),
    ("data_access", "Data access", True),
    ("external", "External calls", True),
    ("persistence", "Domain models / persistence", True),
    ("messaging", "Async / messaging", False),
    ("cross_cutting", "Cross-cutting", False),
)


def list_top_directories(root: Path) -> List[DirectoryRole]:
    """Return labelled top-level directories (sorted, skip-listed)."""
    hint_map = role_hint_map()
    out: List[DirectoryRole] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        if _is_skipped_dir(child.name):
            continue
        role, desc = hint_map.get(child.name, ("other", ""))
        if not desc:
            if any((child / f).is_file() for f in ("__init__.py",)):
                role, desc = "source", "Python package."
            elif (child / "tsconfig.json").exists() or (child / "package.json").exists():
                role, desc = "source", "Node sub-project."
        out.append(
            DirectoryRole(
                name=child.name,
                role=role,
                description=desc,
                parent_dir=None,
                rel_path=child.name,
            )
        )
    return out


def list_nested_directories(
    root: Path,
    parents: Iterable[DirectoryRole],
    *,
    max_per_parent: int = 16,
) -> List[DirectoryRole]:
    """Walk one level deep under *source-role* parents and label children.

    This is what surfaces the architectural folders (``app/services``,
    ``app/clients``, ``app/routes``, …) that hide one level under a single
    top-level package directory.
    """
    hint_map = role_hint_map()
    source_roles = {"source", "frontend", "backend"}
    out: List[DirectoryRole] = []
    for parent in parents:
        if parent.role not in source_roles:
            continue
        parent_path = root / (parent.rel_path or parent.name)
        if not parent_path.is_dir():
            continue
        children: List[DirectoryRole] = []
        for child in sorted(parent_path.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            if _is_skipped_dir(child.name):
                continue
            if child.name in {"__pycache__", "tests", "test", "__tests__"}:
                # Tests under a source dir are still tests, not architecture.
                continue
            role, desc = hint_map.get(child.name, ("other", ""))
            if not desc:
                if (child / "__init__.py").exists():
                    role, desc = "module", "Python sub-package."
            children.append(
                DirectoryRole(
                    name=child.name,
                    role=role,
                    description=desc,
                    parent_dir=parent.name,
                    rel_path=str(child.relative_to(root)),
                )
            )
            if len(children) >= max_per_parent:
                break
        out.extend(children)
    return out


def role_hint_map() -> Dict[str, Tuple[str, str]]:
    return {name: (role, desc) for name, role, desc in _DIR_ROLE_HINTS}


def _is_skipped_dir(name: str) -> bool:
    if name in SKIP_DIRS:
        return True
    if name.endswith((".egg-info", ".dist-info")):
        return True
    if name.startswith(".") and name not in {".github", ".gitlab"}:
        return True
    return False


def file_count_by_extension(root: Path, *, max_files: int = 20_000) -> Tuple[Dict[str, int], int]:
    """Walk the repo (skip-listed) and count files by extension.

    Returns ``(counts_by_ext, total_files_seen)``. Bounded by ``max_files`` to
    keep the scan quick on monorepos; we stop counting once we hit the cap but
    still return useful proportions.
    """
    counts: Dict[str, int] = {}
    total = 0
    for path, dirs, files in _walk(root):
        for fname in files:
            total += 1
            ext = "." + fname.rsplit(".", 1)[1].lower() if "." in fname else ""
            counts[ext] = counts.get(ext, 0) + 1
            if total >= max_files:
                return counts, total
    return counts, total


def _walk(root: Path) -> Iterable[Tuple[str, List[str], List[str]]]:
    import os

    for dirpath, dirnames, filenames in os.walk(str(root)):
        # In-place filter: never descend into skip-listed dirs.
        dirnames[:] = [d for d in dirnames if not _is_skipped_dir(d)]
        yield dirpath, dirnames, filenames


def detect_ci_providers(root: Path) -> List[str]:
    out: List[str] = []
    if (root / ".github" / "workflows").is_dir():
        out.append("GitHub Actions")
    if (root / ".gitlab-ci.yml").exists():
        out.append("GitLab CI")
    if (root / ".circleci" / "config.yml").exists():
        out.append("CircleCI")
    if (root / "azure-pipelines.yml").exists():
        out.append("Azure Pipelines")
    if (root / "Jenkinsfile").exists():
        out.append("Jenkins")
    if (root / ".buildkite" / "pipeline.yml").exists():
        out.append("Buildkite")
    return out


def detect_license(root: Path) -> Optional[str]:
    label_map = {
        "mit license": "MIT",
        "apache": "Apache 2.0",
        "mozilla": "MPL 2.0",
        "bsd": "BSD",
        "gpl": "GPL",
        "isc": "ISC",
        "unlicense": "Unlicense",
    }
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        p = root / name
        if p.is_file():
            try:
                head = p.read_text(encoding="utf-8", errors="replace")[:300].lower()
            except OSError:
                return name
            for needle, friendly in label_map.items():
                if needle in head:
                    return friendly
            return name
    return None


def file_naming_patterns(root: Path, *, source_dirs: Iterable[str]) -> List[Evidence]:
    """Detect common class-suffix patterns in filenames (Manager, Service, etc.).

    Case-insensitive so we pick up snake_case (`validation_service.py`) as well
    as PascalCase (`UserService.java`).
    """
    seen: Dict[str, str] = {}
    targets = ("Manager", "Service", "Client", "Repository", "Controller", "Handler", "Provider", "Factory")
    candidate_dirs = [root / d for d in source_dirs if (root / d).is_dir()] or [root]
    for sd in candidate_dirs:
        for path in sorted(sd.rglob("*")):
            if path.is_dir() or _is_skipped_dir(path.parent.name):
                continue
            tokens = _split_identifier(path.stem)
            for token in targets:
                if token in seen:
                    continue
                if token.lower() in tokens:
                    seen[token] = str(path.relative_to(root))
            if len(seen) == len(targets):
                break
    return [Evidence(fact=f"{token} pattern", source=src) for token, src in sorted(seen.items())]


def _split_identifier(name: str) -> set[str]:
    """Tokenise a filename stem across snake_case, kebab-case, and CamelCase."""
    parts = re.split(r"[_\-\.]", name)
    out: set[str] = set()
    for part in parts:
        # Split CamelCase / PascalCase into runs of capital + lowercase / digits.
        for sub in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?=[A-Z]|$)", part) or [part]:
            if sub:
                out.add(sub.lower())
    return out
