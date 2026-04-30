"""Go language adapter."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from changebrief.core.ai_context.languages.base import LanguageAdapter
from changebrief.core.ai_context.models import Evidence, LanguageProfile


_GO_FRAMEWORK_MAP = {
    "github.com/gin-gonic/gin": "Gin",
    "github.com/labstack/echo": "Echo",
    "github.com/gofiber/fiber": "Fiber",
    "github.com/gorilla/mux": "Gorilla mux",
    "github.com/go-chi/chi": "go-chi",
    "google.golang.org/grpc": "gRPC",
    "github.com/spf13/cobra": "Cobra (CLI)",
    "github.com/urfave/cli": "urfave/cli",
    "gorm.io/gorm": "GORM",
    "github.com/jmoiron/sqlx": "sqlx",
    "github.com/aws/aws-sdk-go": "AWS SDK",
    "github.com/redis/go-redis": "Redis client",
}


class GoAdapter(LanguageAdapter):
    name = "go"
    file_extensions = (".go",)
    config_files = ("go.mod", "go.sum")

    def detect(self, root: Path, files_by_ext: Dict[str, int]) -> bool:
        return files_by_ext.get(".go", 0) > 0 or (root / "go.mod").exists()

    def gather(self, root: Path) -> LanguageProfile:
        profile = LanguageProfile(language=self.name, package_manager="go modules")
        gomod = root / "go.mod"
        if gomod.exists():
            text = self._read(gomod)
            seen: set[str] = set()
            # Match both `require pkg v1.2.3` and bare-pkg lines inside a `require ( ... )` block.
            req_re = re.compile(r"^(?:require\s+)?([\w.\-/]+)\s+v[\w.\-+]+")
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("//") or stripped in {"require (", ")"}:
                    continue
                m = req_re.match(stripped)
                if not m:
                    continue
                imp = m.group(1)
                for prefix, friendly in _GO_FRAMEWORK_MAP.items():
                    if imp.startswith(prefix) and friendly not in seen:
                        seen.add(friendly)
                        profile.frameworks.append(Evidence(fact=friendly, source="go.mod"))

        if any(p.is_dir() and p.name == "cmd" for p in root.iterdir()):
            profile.source_dirs.append("cmd")
            profile.extra_notes.append(
                Evidence(fact="Entry-point binaries live under `cmd/`", source="cmd/")
            )
        for d in ("internal", "pkg", "api", "service", "services"):
            if (root / d).is_dir():
                profile.source_dirs.append(d)
        # Go tests are colocated; there's no `tests/` convention.
        profile.test_framework = "go test (stdlib)"
        return profile
