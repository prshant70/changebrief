"""Rust language adapter."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from changebrief.core.ai_context.languages.base import LanguageAdapter
from changebrief.core.ai_context.models import Evidence, LanguageProfile


_RUST_FRAMEWORK_MAP = {
    "actix-web": "Actix Web",
    "axum": "Axum",
    "rocket": "Rocket",
    "warp": "warp",
    "tower": "Tower",
    "tonic": "tonic (gRPC)",
    "tokio": "Tokio (async runtime)",
    "async-std": "async-std",
    "serde": "serde",
    "sqlx": "sqlx",
    "diesel": "Diesel (ORM)",
    "sea-orm": "SeaORM",
    "clap": "clap (CLI)",
    "anyhow": "anyhow (errors)",
    "thiserror": "thiserror (errors)",
}


class RustAdapter(LanguageAdapter):
    name = "rust"
    file_extensions = (".rs",)
    config_files = ("Cargo.toml", "Cargo.lock")

    def detect(self, root: Path, files_by_ext: Dict[str, int]) -> bool:
        return files_by_ext.get(".rs", 0) > 0 or (root / "Cargo.toml").exists()

    def gather(self, root: Path) -> LanguageProfile:
        profile = LanguageProfile(language=self.name, package_manager="cargo")
        cargo = root / "Cargo.toml"
        if cargo.exists():
            text = self._read(cargo)
            section = re.search(
                r"^\[(?:dependencies|dev-dependencies)\](.*?)(?=^\[|\Z)",
                text,
                re.DOTALL | re.MULTILINE,
            )
            if section:
                for line in section.group(1).splitlines():
                    m = re.match(r"^\s*([a-zA-Z0-9_\-]+)\s*=", line)
                    if not m:
                        continue
                    name = m.group(1)
                    friendly = _RUST_FRAMEWORK_MAP.get(name)
                    if friendly:
                        profile.frameworks.append(
                            Evidence(fact=friendly, source="Cargo.toml")
                        )
        if (root / "src").is_dir():
            profile.source_dirs.append("src")
        if (root / "tests").is_dir():
            profile.test_dirs.append("tests")
        profile.test_framework = "cargo test"
        return profile
