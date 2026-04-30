"""Python language adapter."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from changebrief.core.ai_context.languages.base import LanguageAdapter
from changebrief.core.ai_context.models import Evidence, LanguageProfile


# Friendly framework / library names by PyPI distribution name.
_PY_FRAMEWORK_MAP: Dict[str, str] = {
    "fastapi": "FastAPI",
    "flask": "Flask",
    "django": "Django",
    "sanic": "Sanic",
    "starlette": "Starlette",
    "tornado": "Tornado",
    "aiohttp": "aiohttp",
    "bottle": "Bottle",
    "quart": "Quart",
    "litestar": "Litestar",
    "typer": "Typer (CLI)",
    "click": "Click (CLI)",
    "celery": "Celery (task queue)",
    "rq": "RQ (task queue)",
    "redis": "Redis client",
    "sqlalchemy": "SQLAlchemy",
    "tortoise-orm": "Tortoise ORM",
    "asyncpg": "asyncpg",
    "psycopg": "psycopg",
    "psycopg2": "psycopg2",
    "psycopg2-binary": "psycopg2",
    "alembic": "Alembic (migrations)",
    "pydantic": "Pydantic",
    "pydantic-settings": "pydantic-settings",
    "openai": "OpenAI SDK",
    "anthropic": "Anthropic SDK",
    "boto3": "AWS SDK (boto3)",
    "google-cloud-storage": "Google Cloud Storage",
    "kafka-python": "Kafka client",
    "confluent-kafka": "Kafka client (Confluent)",
    "pyyaml": "PyYAML",
    "httpx": "httpx",
    "requests": "requests",
}

_PY_TEST_FRAMEWORKS = {"pytest", "unittest", "nose", "nose2", "ward"}

_PY_ROUTE_PATTERNS = [
    re.compile(
        r"@(?:app|router|api|blueprint|bp)\.(?:get|post|put|delete|patch|route|websocket)\s*\([^)]*\)"
    ),
    re.compile(r"\b(?:path|re_path|url)\s*\(\s*[r]?[\"'][^\"']+[\"']"),  # Django
]

# Conservative caps to keep the scan fast on large repos.
_MAX_PY_FILES_FOR_ENTRY_POINTS = 250
_MAX_ENTRY_POINTS = 12


class PythonAdapter(LanguageAdapter):
    name = "python"
    file_extensions = (".py", ".pyi")
    config_files = (
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "Pipfile",
        "poetry.lock",
        "uv.lock",
    )

    def detect(self, root: Path, files_by_ext: Dict[str, int]) -> bool:
        if files_by_ext.get(".py", 0) > 0:
            return True
        return any((root / cf).exists() for cf in self.config_files)

    def gather(self, root: Path) -> LanguageProfile:
        profile = LanguageProfile(language=self.name)

        deps = self._collect_dependencies(root)
        seen_fw: set[str] = set()
        declared: set[str] = set()
        for dep_name, dep_source in deps:
            base = self._normalise_dist(dep_name)
            if base:
                declared.add(base)
            friendly = _PY_FRAMEWORK_MAP.get(base)
            if friendly and friendly not in seen_fw:
                seen_fw.add(friendly)
                profile.frameworks.append(Evidence(fact=friendly, source=dep_source))
            if base in _PY_TEST_FRAMEWORKS and not profile.test_framework:
                profile.test_framework = base
        profile.declared_dependencies = sorted(declared)

        # Package manager detection.
        for marker, pm in [
            ("uv.lock", "uv"),
            ("poetry.lock", "poetry"),
            ("Pipfile.lock", "pipenv"),
            ("requirements.txt", "pip"),
        ]:
            if (root / marker).exists():
                profile.package_manager = pm
                break
        if not profile.package_manager and (root / "pyproject.toml").exists():
            profile.package_manager = "pip (PEP 517)"

        # Source directories: src/, lib/, app/, apps/, plus top-level packages.
        for d in ("src", "lib", "app", "apps"):
            if (root / d).is_dir():
                profile.source_dirs.append(d)
        for child in sorted(root.iterdir()):
            if (
                child.is_dir()
                and not child.name.startswith(".")
                and (child / "__init__.py").exists()
                and child.name not in profile.source_dirs
            ):
                profile.source_dirs.append(child.name)

        # Test directories.
        for d in ("tests", "test", "testing"):
            if (root / d).is_dir():
                profile.test_dirs.append(d)

        # Project scripts (CLI entry points declared in pyproject.toml).
        profile.run_scripts.update(self._project_scripts(root))
        # Common local-dev commands inferred from declared tooling.
        profile.run_scripts.update(
            self._tooling_scripts(
                root=root,
                declared=declared,
                package_manager=profile.package_manager,
                has_tests_dir=bool(profile.test_dirs),
            )
        )

        # Route-style entry points discovered in source.
        profile.entry_points = self._scan_entry_points(root, profile.source_dirs)

        return profile

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _normalise_dist(name: str) -> str:
        # Strip extras, version specifiers, environment markers.
        base = name.split(";")[0].strip()
        base = re.split(r"[<>=!~]", base, maxsplit=1)[0]
        base = base.split("[")[0]
        return base.strip().lower().replace("_", "-")

    def _collect_dependencies(self, root: Path) -> List[tuple[str, str]]:
        """Return [(distribution_name, source_path)] across pyproject + requirements + Pipfile."""
        out: List[tuple[str, str]] = []

        pp = root / "pyproject.toml"
        if pp.exists():
            text = self._read(pp)
            # [project].dependencies = [...]
            for m in re.finditer(
                r"^\s*dependencies\s*=\s*\[(.*?)\]",
                text,
                re.DOTALL | re.MULTILINE | re.IGNORECASE,
            ):
                for s in re.finditer(r"\"([^\"]+)\"|'([^']+)'", m.group(1)):
                    out.append((s.group(1) or s.group(2), "pyproject.toml:dependencies"))
            # [project.optional-dependencies] block
            for m in re.finditer(
                r"^\s*\[project\.optional-dependencies\](.*?)(?=^\[|\Z)",
                text,
                re.DOTALL | re.MULTILINE,
            ):
                for s in re.finditer(r"\"([^\"]+)\"|'([^']+)'", m.group(1)):
                    out.append(
                        (s.group(1) or s.group(2), "pyproject.toml:optional-dependencies")
                    )

        rt = root / "requirements.txt"
        if rt.exists():
            for line in self._read(rt).splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("-"):
                    out.append((line, "requirements.txt"))

        pf = root / "Pipfile"
        if pf.exists():
            out.extend(self._collect_dependencies_pipfile(pf))

        return out

    def _collect_dependencies_pipfile(self, pipfile: Path) -> List[tuple[str, str]]:
        """Parse Pipfile for [packages] and [dev-packages] dependency names."""
        try:
            import tomllib  # py3.11+
        except Exception:
            return []
        try:
            data = tomllib.loads(self._read(pipfile) or "")
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        out: List[tuple[str, str]] = []
        for section, source in (("packages", "Pipfile:packages"), ("dev-packages", "Pipfile:dev-packages")):
            block = data.get(section) or {}
            if not isinstance(block, dict):
                continue
            for name in block.keys():
                pkg = str(name or "").strip()
                if pkg:
                    out.append((pkg, source))
        return out

    def _project_scripts(self, root: Path) -> Dict[str, str]:
        scripts: Dict[str, str] = {}
        pp = root / "pyproject.toml"
        if not pp.exists():
            return scripts
        text = self._read(pp)
        m = re.search(
            r"^\[project\.scripts\](.*?)(?=^\[|\Z)",
            text,
            re.DOTALL | re.MULTILINE,
        )
        if not m:
            return scripts
        for line in m.group(1).splitlines():
            if "=" not in line:
                continue
            name, _, raw = line.partition("=")
            name = name.strip().strip('"').strip("'")
            cmd = raw.strip().strip('"').strip("'")
            if name and cmd and not name.startswith("#"):
                scripts[name] = cmd
        return scripts

    @staticmethod
    def _tooling_scripts(
        *,
        root: Path,
        declared: set[str],
        package_manager: str | None,
        has_tests_dir: bool,
    ) -> Dict[str, str]:
        """Infer conventional dev scripts from common tooling deps.

        We keep these conservative and broadly portable. If a repo has explicit
        scripts (Makefile/package.json/pyproject scripts), those take precedence.
        """
        prefix = ""
        pm = (package_manager or "").strip().lower()
        if pm == "poetry":
            prefix = "poetry run "
        elif pm == "pipenv":
            prefix = "pipenv run "
        elif pm == "uv":
            prefix = "uv run "

        def cmd(raw: str) -> str:
            return f"{prefix}{raw}".strip()

        out: Dict[str, str] = {}

        def exists_any(*paths: str) -> bool:
            return any((root / p).exists() for p in paths)

        # Tests
        if ("pytest" in declared or exists_any("pytest.ini", "pyproject.toml", "tox.ini")) and has_tests_dir:
            out["tests"] = cmd("pytest -q")

        # Linting
        if "ruff" in declared or exists_any("ruff.toml", ".ruff.toml"):
            out["lint"] = cmd("ruff check .")

        # Formatting
        # Prefer ruff-format when available; otherwise fall back to black.
        if "ruff" in declared or exists_any("ruff.toml", ".ruff.toml"):
            out["format"] = cmd("ruff format .")
        elif "black" in declared:
            out["format"] = cmd("black .")

        # Typechecking
        if "mypy" in declared or exists_any("mypy.ini", ".mypy.ini"):
            out["typecheck"] = cmd("mypy .")
        elif "pyright" in declared:
            out["typecheck"] = cmd("pyright")

        # Pre-commit
        if "pre-commit" in declared or exists_any(".pre-commit-config.yaml"):
            out["precommit"] = cmd("pre-commit run --all-files")

        return out

    def _scan_entry_points(self, root: Path, source_dirs: List[str]) -> List[str]:
        scan_roots = [root / d for d in source_dirs if (root / d).is_dir()] or [root]
        seen: set[tuple[str, str]] = set()
        out: List[str] = []
        files_seen = 0
        for sr in scan_roots:
            for path in sorted(sr.rglob("*.py")):
                files_seen += 1
                if files_seen > _MAX_PY_FILES_FOR_ENTRY_POINTS:
                    return out
                if any(
                    part in {".venv", "venv", "__pycache__", ".tox", ".mypy_cache"}
                    for part in path.parts
                ):
                    continue
                text = self._read(path)
                for pat in _PY_ROUTE_PATTERNS:
                    for m in pat.finditer(text):
                        rel = str(path.relative_to(root))
                        snippet = m.group(0).strip()
                        key = (rel, snippet)
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(f"{rel}: {snippet}")
                        if len(out) >= _MAX_ENTRY_POINTS:
                            return out
        return out
