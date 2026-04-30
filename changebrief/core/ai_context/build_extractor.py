"""Deterministic, AST-driven extractor for `ai-context build`.

Goal: collect everything an AI coding agent needs to write *idiomatic* code
against a framework / utility — without making any LLM call. The output of
this module is the "ground truth" that the (optional) LLM synthesis pass
turns into prose for the home-level ``context.yaml``.

What we collect (Python-first; gracefully degrades for other languages):

* **Public API surface** — names listed in ``__all__`` of the package's top
  ``__init__.py`` (or, when missing, top-level non-underscore symbols).
* **Exception family** — classes whose base contains ``Exception`` or
  ``Error``, with the line they're declared on.
* **Decorators** — module-level functions whose return value is itself a
  callable (heuristic) plus any function decorated with ``@<name>``.
* **Notable directories** — direct children of the package source dir
  whose name matches well-known framework concepts (``api_clients``,
  ``exceptions``, ``middlewares``, ``listeners``, ``circuit_breaker``, …).
* **Examples** — files under ``examples/`` (truncated by line count).
* **Docs excerpts** — first chunks of ``README.md`` and ``docs/index.md``.
* **Config schema hints** — ``config.json`` keys at the repo root (if any),
  ``CONFIG`` references in source, and environment-variable lookups.
* **Python version pin** — from ``pyproject.toml``.

Every fact carries a ``source`` so the synthesizer can cite real paths and
the verifier can drop hallucinated ones.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from changebrief.core.ai_context.models import RepoContext


# Limits — keep extraction snappy and the LLM prompt below context budget.
_MAX_PY_FILES_FOR_AST = 250
_MAX_EXAMPLE_FILES = 3
_EXAMPLE_FILE_MAX_BYTES = 4_000
_README_MAX_CHARS = 4_000
_DOC_FILE_MAX_CHARS = 3_000
_CONFIG_JSON_MAX_KEYS = 60

# Names of subdirectories that hint at a "framework concept worth knowing".
# Used both to surface them as notes and to hand them to the LLM as
# context. Not exhaustive — we err on the side of letting through.
_NOTABLE_SUBDIR_HINTS: Dict[str, str] = {
    "api_clients": "Outbound HTTP clients (BaseAPIClient pattern).",
    "clients": "Outbound clients for external services.",
    "exceptions": "Custom exception types raised by the framework.",
    "errors": "Custom error types raised by the framework.",
    "middlewares": "Request/response middleware pipeline.",
    "middleware": "Request/response middleware pipeline.",
    "listeners": "Lifecycle listeners (startup/shutdown).",
    "decorators": "Reusable function decorators.",
    "circuit_breaker": "Circuit-breaker primitives (failure isolation).",
    "task_executor": "Concurrent task executor primitives.",
    "tasks": "Background-task primitives.",
    "response": "Response envelope helpers.",
    "responses": "Response envelope helpers.",
    "request": "Request-side helpers.",
    "logging": "Structured-logging configuration.",
    "tracing": "Tracing / OpenTelemetry wiring.",
    "telemetry": "Telemetry / metrics integration.",
    "config": "Configuration loading.",
    "utils": "Utility helpers (read before adding a new helper).",
    "models": "Domain models.",
    "schemas": "Request/response schema definitions.",
    "serializers": "Serialization helpers.",
    "validators": "Input validators.",
}


@dataclass
class PublicSymbol:
    """A symbol exposed on the package's top-level namespace."""

    name: str
    kind: str  # "class" | "function" | "constant" | "module"
    source: str  # repo-relative path of the file that defines the name


@dataclass
class ExceptionInfo:
    name: str
    bases: List[str]
    source: str  # repo-relative path:line


@dataclass
class DecoratorInfo:
    name: str
    source: str  # repo-relative path:line


@dataclass
class NotableDir:
    name: str
    description: str
    rel_path: str  # repo-relative path


@dataclass
class ExampleFile:
    rel_path: str
    content: str  # truncated head of the file


@dataclass
class FrameworkExtraction:
    """Everything the deterministic pass managed to learn about the framework.

    Used by both the markdown-free YAML writer (for the no-LLM fallback) and
    by the LLM prompt (as ground-truth context).
    """

    package_name: str  # the import name (lowercase, hyphenated)
    project_name: str
    summary: Optional[str]
    primary_language: Optional[str]
    framework_facts: List[str]  # short, citation-bearing factual lines
    public_api: List[PublicSymbol]
    exceptions: List[ExceptionInfo]
    decorators: List[DecoratorInfo]
    notable_dirs: List[NotableDir]
    examples: List[ExampleFile]
    readme_excerpt: Optional[str]
    doc_excerpts: List[Tuple[str, str]] = field(default_factory=list)  # (rel_path, text)
    config_keys: List[str] = field(default_factory=list)
    python_version_pin: Optional[str] = None
    repo_root: str = ""
    package_dir: Optional[str] = None  # e.g. "torpedo"
    sample_paths: List[str] = field(default_factory=list)  # files we cite as evidence


def extract_framework(repo_ctx: RepoContext, *, package_name: str) -> FrameworkExtraction:
    """Run the deterministic extraction pass for a framework repo.

    ``package_name`` is the canonical (lowercase) import name decided by
    the caller (CLI ``--name`` override or auto-detection).
    """
    root = Path(repo_ctx.root)
    profiles = [p for p in repo_ctx.profiles if p.language != "generic"]
    py_profile = next((p for p in profiles if p.language == "python"), None)

    package_dir = _find_package_dir(root, package_name, py_profile.source_dirs if py_profile else [])

    public_api: List[PublicSymbol] = []
    exceptions: List[ExceptionInfo] = []
    decorators: List[DecoratorInfo] = []
    if package_dir is not None:
        public_api = _extract_public_api(root, package_dir)
        exceptions, decorators = _extract_exceptions_and_decorators(root, package_dir)

    notable = _detect_notable_dirs(root, package_dir)
    examples = _read_examples(root)
    readme = _read_readme(root)
    docs = _read_docs(root)
    config_keys = _detect_config_keys(root)
    py_pin = _detect_python_pin(root)

    sample_paths = _collect_sample_paths(
        root,
        package_dir=package_dir,
        public_api=public_api,
        exceptions=exceptions,
        notable=notable,
        examples=examples,
        docs=docs,
    )

    facts = _compose_facts(
        repo_ctx=repo_ctx,
        package_dir=package_dir,
        py_pin=py_pin,
        notable=notable,
        public_api=public_api,
        exceptions=exceptions,
        config_keys=config_keys,
    )

    return FrameworkExtraction(
        package_name=package_name,
        project_name=repo_ctx.project_name or package_name,
        summary=repo_ctx.project_summary,
        primary_language=repo_ctx.primary_language or (py_profile.language if py_profile else None),
        framework_facts=facts,
        public_api=public_api,
        exceptions=exceptions,
        decorators=decorators,
        notable_dirs=notable,
        examples=examples,
        readme_excerpt=readme,
        doc_excerpts=docs,
        config_keys=config_keys,
        python_version_pin=py_pin,
        repo_root=str(root),
        package_dir=package_dir,
        sample_paths=sample_paths,
    )


# ---------------------------------------------------------------------------- helpers


def _find_package_dir(root: Path, package_name: str, source_dirs: Iterable[str]) -> Optional[str]:
    """Locate the actual import-name directory inside ``root``.

    ``package_name`` may use hyphens (the YAML key form); the import dir on
    disk uses underscores. We try both.
    """
    underscore = package_name.replace("-", "_")
    candidates: List[str] = []
    for src in source_dirs:
        top = src.split("/", 1)[0]
        if top in {"src", "lib", "app", "apps"}:
            continue
        candidates.append(top)
    for c in candidates:
        if c.lower() == underscore.lower():
            return c
    if candidates:
        return candidates[0]

    # Manual probe: ``<root>/<name>/__init__.py`` or ``<root>/src/<name>/__init__.py``.
    for prefix in ("", "src/"):
        for variant in (underscore, package_name):
            cand = root / f"{prefix}{variant}"
            if (cand / "__init__.py").exists():
                rel = str(cand.relative_to(root))
                return rel
    return None


def _extract_public_api(root: Path, package_dir: str) -> List[PublicSymbol]:
    init_path = root / package_dir / "__init__.py"
    if not init_path.is_file():
        return []
    try:
        text = init_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    rel = str(init_path.relative_to(root))

    # Prefer __all__ when present. It's the framework author's curated surface.
    all_names = _read_dunder_all(tree)
    if all_names:
        defined_in = _resolve_imports(tree)
        out: List[PublicSymbol] = []
        for name in all_names:
            origin = defined_in.get(name) or rel
            kind = _guess_kind(tree, name)
            out.append(PublicSymbol(name=name, kind=kind, source=origin))
        return out

    # Fall back: top-level non-underscore defs/assignments.
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            out.append(PublicSymbol(name=node.name, kind="function", source=rel))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            out.append(PublicSymbol(name=node.name, kind="class", source=rel))
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and not tgt.id.startswith("_"):
                    out.append(PublicSymbol(name=tgt.id, kind="constant", source=rel))
    return out[:30]


def _read_dunder_all(tree: ast.Module) -> List[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                    return _literal_string_list(node.value)
    return []


def _literal_string_list(node: ast.AST) -> List[str]:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out: List[str] = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
        return out
    return []


def _resolve_imports(tree: ast.Module) -> Dict[str, str]:
    """Map ``Symbol -> "<pkg>.<module>"`` for ``from x.y import Symbol`` lines.

    We only use this to attach a hint about *where the symbol came from*;
    callers fall back to ``__init__.py`` itself when the source can't be
    pinned to a relative file path.
    """
    out: Dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                name = alias.asname or alias.name
                if name == "*":
                    continue
                out[name] = module
    return out


def _guess_kind(tree: ast.Module, name: str) -> str:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "function"
        if isinstance(node, ast.ClassDef) and node.name == name:
            return "class"
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return "constant"
    return "module"


def _extract_exceptions_and_decorators(
    root: Path, package_dir: str,
) -> Tuple[List[ExceptionInfo], List[DecoratorInfo]]:
    """Walk the package and pull exception classes + decorator names out of AST."""
    pkg_path = root / package_dir
    excs: List[ExceptionInfo] = []
    decs: List[DecoratorInfo] = []
    seen_dec: Set[str] = set()

    files_seen = 0
    for path in sorted(pkg_path.rglob("*.py")):
        files_seen += 1
        if files_seen > _MAX_PY_FILES_FOR_AST:
            break
        if any(part in {"__pycache__", "tests", "test"} for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = str(path.relative_to(root))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _is_exception_class(node):
                bases = [_unparse_base(b) for b in node.bases]
                bases = [b for b in bases if b]
                excs.append(
                    ExceptionInfo(
                        name=node.name,
                        bases=bases,
                        source=f"{rel}:{node.lineno}",
                    )
                )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _looks_like_decorator(node) and node.name not in seen_dec:
                    seen_dec.add(node.name)
                    decs.append(DecoratorInfo(name=node.name, source=f"{rel}:{node.lineno}"))

    excs.sort(key=lambda e: e.name.lower())
    decs.sort(key=lambda d: d.name.lower())
    return excs[:40], decs[:20]


def _is_exception_class(node: ast.ClassDef) -> bool:
    if node.name.lower().endswith(("exception", "error")):
        return True
    for base in node.bases:
        text = _unparse_base(base) or ""
        if text.endswith(("Exception", "Error")):
            return True
    return False


def _unparse_base(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover — defensive
        return ""


def _looks_like_decorator(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Heuristic: a top-level function whose body returns an inner function/wrapper.

    We deliberately don't try to be too clever — the goal is to surface
    *candidates* for the LLM to confirm via the README and source.
    """
    if any(d for d in func.decorator_list if isinstance(d, ast.Name) and d.id in {"staticmethod", "classmethod"}):
        return False
    for stmt in func.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in func.body:
                if isinstance(inner, ast.Return):
                    val = inner.value
                    if isinstance(val, ast.Name):
                        return True
            return True
    # `def wrapper(*a, **k): return f(*a, **k)` — a decorator with no inner def.
    return False


def _detect_notable_dirs(root: Path, package_dir: Optional[str]) -> List[NotableDir]:
    out: List[NotableDir] = []
    if package_dir is None:
        return out
    pkg = root / package_dir
    if not pkg.is_dir():
        return out
    for child in sorted(pkg.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name == "__pycache__":
            continue
        desc = _NOTABLE_SUBDIR_HINTS.get(child.name)
        if not desc:
            continue
        out.append(
            NotableDir(
                name=child.name,
                description=desc,
                rel_path=str(child.relative_to(root)),
            )
        )
    return out


def _read_examples(root: Path) -> List[ExampleFile]:
    out: List[ExampleFile] = []
    candidates = [root / "examples", root / "example"]
    for ex_dir in candidates:
        if not ex_dir.is_dir():
            continue
        for path in sorted(ex_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix not in {".py", ".md", ".rst", ".js", ".ts"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(text) > _EXAMPLE_FILE_MAX_BYTES:
                text = text[:_EXAMPLE_FILE_MAX_BYTES] + "\n... [truncated] ...\n"
            out.append(ExampleFile(rel_path=str(path.relative_to(root)), content=text))
            if len(out) >= _MAX_EXAMPLE_FILES:
                return out
    return out


def _read_readme(root: Path) -> Optional[str]:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        p = root / name
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8", errors="replace")[:_README_MAX_CHARS]
            except OSError:
                return None
    return None


def _read_docs(root: Path) -> List[Tuple[str, str]]:
    """Pick up to 3 short docs files (index, README inside docs, getting started)."""
    out: List[Tuple[str, str]] = []
    docs_dir = root / "docs"
    if not docs_dir.is_dir():
        return out
    preferred = ["index.md", "README.md", "getting-started.md", "quickstart.md", "overview.md"]
    seen: Set[str] = set()
    for name in preferred:
        path = docs_dir / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:_DOC_FILE_MAX_CHARS]
            except OSError:
                continue
            rel = str(path.relative_to(root))
            seen.add(rel)
            out.append((rel, text))
            if len(out) >= 3:
                return out
    if len(out) < 3:
        for path in sorted(docs_dir.glob("*.md")):
            rel = str(path.relative_to(root))
            if rel in seen:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:_DOC_FILE_MAX_CHARS]
            except OSError:
                continue
            out.append((rel, text))
            if len(out) >= 3:
                break
    return out


def _detect_config_keys(root: Path) -> List[str]:
    """Read ``config.json`` (when present) and return its top-level keys."""
    cfg = root / "config.json"
    if not cfg.is_file():
        return []
    try:
        data = json.loads(cfg.read_text(encoding="utf-8", errors="replace") or "{}")
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    keys = list(data.keys())
    return keys[:_CONFIG_JSON_MAX_KEYS]


_PYTHON_PIN_RE = re.compile(
    r"^\s*requires-python\s*=\s*['\"]([^'\"]+)['\"]",
    re.MULTILINE | re.IGNORECASE,
)


def _detect_python_pin(root: Path) -> Optional[str]:
    pp = root / "pyproject.toml"
    if not pp.is_file():
        return None
    try:
        text = pp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _PYTHON_PIN_RE.search(text)
    return m.group(1).strip() if m else None


def _collect_sample_paths(
    root: Path,
    *,
    package_dir: Optional[str],
    public_api: List[PublicSymbol],
    exceptions: List[ExceptionInfo],
    notable: List[NotableDir],
    examples: List[ExampleFile],
    docs: List[Tuple[str, str]],
) -> List[str]:
    """The set of repo-relative paths the LLM is allowed to cite."""
    paths: Set[str] = set()
    if package_dir:
        for cand in ("__init__.py", "response.py", "exceptions.py"):
            p = root / package_dir / cand
            if p.is_file():
                paths.add(str(p.relative_to(root)))
    for sym in public_api:
        paths.add(sym.source)
    for exc in exceptions:
        paths.add(exc.source.split(":", 1)[0])
    for nd in notable:
        paths.add(nd.rel_path)
    for ex in examples:
        paths.add(ex.rel_path)
    for rel, _ in docs:
        paths.add(rel)
    if (root / "README.md").is_file():
        paths.add("README.md")
    if (root / "pyproject.toml").is_file():
        paths.add("pyproject.toml")
    if (root / "config.json").is_file():
        paths.add("config.json")
    return sorted(paths)


def _compose_facts(
    *,
    repo_ctx: RepoContext,
    package_dir: Optional[str],
    py_pin: Optional[str],
    notable: List[NotableDir],
    public_api: List[PublicSymbol],
    exceptions: List[ExceptionInfo],
    config_keys: List[str],
) -> List[str]:
    """Short, citation-bearing factual lines for the synthesizer's prompt."""
    facts: List[str] = []
    if py_pin:
        facts.append(f"Python version pin: `{py_pin}` (pyproject.toml).")
    if package_dir:
        facts.append(f"Package source: `{package_dir}/`.")
    if public_api:
        names = ", ".join(f"`{s.name}`" for s in public_api[:18])
        facts.append(f"Public API (from `__init__.py` `__all__`): {names}.")
    if exceptions:
        names = ", ".join(f"`{e.name}`" for e in exceptions[:14])
        facts.append(f"Exception classes: {names}.")
    if notable:
        bits = ", ".join(f"`{nd.rel_path}`" for nd in notable)
        facts.append(f"Notable subdirectories: {bits}.")
    if config_keys:
        facts.append(
            "Top-level `config.json` keys: "
            + ", ".join(f"`{k}`" for k in config_keys[:14])
            + "."
        )
    fws = sorted({ev.fact for p in repo_ctx.profiles for ev in p.frameworks})
    if fws:
        facts.append("Built on: " + ", ".join(fws[:6]) + ".")
    return facts
