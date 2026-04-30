"""JavaScript / TypeScript / Node ecosystem adapter."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

from changebrief.core.ai_context.languages.base import LanguageAdapter
from changebrief.core.ai_context.models import Evidence, LanguageProfile


_NODE_FRAMEWORKS = {
    "express": "Express",
    "koa": "Koa",
    "fastify": "Fastify",
    "@nestjs/core": "NestJS",
    "next": "Next.js",
    "nuxt": "Nuxt",
    "react": "React",
    "vue": "Vue",
    "@angular/core": "Angular",
    "svelte": "Svelte",
    "astro": "Astro",
    "remix": "Remix",
    "vite": "Vite",
    "webpack": "Webpack",
    "graphql": "GraphQL",
    "apollo-server": "Apollo Server",
    "prisma": "Prisma (ORM)",
    "typeorm": "TypeORM",
    "sequelize": "Sequelize",
    "mongoose": "Mongoose",
    "knex": "Knex",
    "axios": "axios",
    "openai": "OpenAI SDK",
    "@anthropic-ai/sdk": "Anthropic SDK",
    "zod": "Zod (schema validation)",
}

_NODE_TEST_FRAMEWORKS = {"jest", "vitest", "mocha", "ava", "tap", "playwright", "@playwright/test", "cypress"}

_JS_ROUTE_PATTERNS = [
    re.compile(r"\b(?:app|router)\.(?:get|post|put|delete|patch|use|all)\s*\([^)]*\)"),
    re.compile(
        r"@(?:Get|Post|Put|Delete|Patch|All|Controller)\s*\([^)]*\)"
    ),  # NestJS
]


def _is_js_or_ts(files_by_ext: Dict[str, int]) -> bool:
    return any(
        files_by_ext.get(ext, 0) > 0
        for ext in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
    )


class _NodeAdapterBase(LanguageAdapter):
    """Shared logic between JS and TS adapters."""

    config_files = ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb")

    def _detect_pm(self, root: Path) -> str | None:
        for marker, pm in [
            ("pnpm-lock.yaml", "pnpm"),
            ("yarn.lock", "yarn"),
            ("bun.lockb", "bun"),
            ("package-lock.json", "npm"),
        ]:
            if (root / marker).exists():
                return pm
        return "npm" if (root / "package.json").exists() else None

    def _read_pkg(self, root: Path) -> dict:
        pkg = root / "package.json"
        if not pkg.exists():
            return {}
        try:
            return json.loads(self._read(pkg) or "{}")
        except json.JSONDecodeError:
            return {}

    def _frameworks_and_test_fw(self, pkg: dict) -> tuple[List[Evidence], str | None]:
        fws: List[Evidence] = []
        deps = {}
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            deps.update(pkg.get(key, {}) or {})
        seen: set[str] = set()
        for dep_name in deps:
            friendly = _NODE_FRAMEWORKS.get(dep_name)
            if friendly and friendly not in seen:
                seen.add(friendly)
                fws.append(Evidence(fact=friendly, source=f"package.json:{dep_name}"))
        test_fw = next(
            (d for d in deps if d in _NODE_TEST_FRAMEWORKS), None
        )
        return fws, test_fw

    def _scan_entry_points(self, root: Path, exts: tuple[str, ...]) -> List[str]:
        seen: set[tuple[str, str]] = set()
        out: List[str] = []
        files_seen = 0
        for ext in exts:
            for path in sorted(root.rglob(f"*{ext}")):
                files_seen += 1
                if files_seen > 250:
                    return out
                if any(
                    part in {"node_modules", ".next", ".nuxt", "dist", "build", ".cache"}
                    for part in path.parts
                ):
                    continue
                text = self._read(path)
                for pat in _JS_ROUTE_PATTERNS:
                    for m in pat.finditer(text):
                        rel = str(path.relative_to(root))
                        snip = m.group(0).strip()
                        key = (rel, snip)
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(f"{rel}: {snip}")
                        if len(out) >= 12:
                            return out
        return out


class JavaScriptAdapter(_NodeAdapterBase):
    name = "javascript"
    file_extensions = (".js", ".jsx", ".mjs", ".cjs")

    def detect(self, root: Path, files_by_ext: Dict[str, int]) -> bool:
        if any(files_by_ext.get(ext, 0) > 0 for ext in self.file_extensions):
            return not (root / "tsconfig.json").exists() or files_by_ext.get(".js", 0) > 0
        return False

    def gather(self, root: Path) -> LanguageProfile:
        profile = LanguageProfile(language=self.name)
        profile.package_manager = self._detect_pm(root)
        pkg = self._read_pkg(root)
        profile.frameworks, profile.test_framework = self._frameworks_and_test_fw(pkg)
        profile.run_scripts = dict((pkg.get("scripts") or {}).items())

        for d in ("src", "lib", "app", "pages", "routes", "server"):
            if (root / d).is_dir():
                profile.source_dirs.append(d)
        for d in ("tests", "test", "__tests__", "spec"):
            if (root / d).is_dir():
                profile.test_dirs.append(d)

        profile.entry_points = self._scan_entry_points(root, self.file_extensions)
        return profile


class TypeScriptAdapter(_NodeAdapterBase):
    name = "typescript"
    file_extensions = (".ts", ".tsx")

    def detect(self, root: Path, files_by_ext: Dict[str, int]) -> bool:
        return any(files_by_ext.get(ext, 0) > 0 for ext in self.file_extensions) or (
            root / "tsconfig.json"
        ).exists()

    def gather(self, root: Path) -> LanguageProfile:
        profile = LanguageProfile(language=self.name)
        profile.package_manager = self._detect_pm(root)
        pkg = self._read_pkg(root)
        profile.frameworks, profile.test_framework = self._frameworks_and_test_fw(pkg)
        profile.run_scripts = dict((pkg.get("scripts") or {}).items())

        if (root / "tsconfig.json").exists():
            profile.extra_notes.append(
                Evidence(fact="TypeScript configured via tsconfig.json", source="tsconfig.json")
            )

        for d in ("src", "lib", "app", "pages", "routes", "server"):
            if (root / d).is_dir():
                profile.source_dirs.append(d)
        for d in ("tests", "test", "__tests__", "spec"):
            if (root / d).is_dir():
                profile.test_dirs.append(d)

        profile.entry_points = self._scan_entry_points(root, self.file_extensions)
        return profile
