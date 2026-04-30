"""Ruby language adapter (light-weight)."""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from changebrief.core.ai_context.languages.base import LanguageAdapter
from changebrief.core.ai_context.models import Evidence, LanguageProfile


_RUBY_FRAMEWORK_MAP = {
    "rails": "Ruby on Rails",
    "sinatra": "Sinatra",
    "hanami": "Hanami",
    "rack": "Rack",
    "rspec": "RSpec",
    "minitest": "Minitest",
}


class RubyAdapter(LanguageAdapter):
    name = "ruby"
    file_extensions = (".rb",)
    config_files = ("Gemfile", "Gemfile.lock", "*.gemspec")

    def detect(self, root: Path, files_by_ext: Dict[str, int]) -> bool:
        return files_by_ext.get(".rb", 0) > 0 or (root / "Gemfile").exists()

    def gather(self, root: Path) -> LanguageProfile:
        profile = LanguageProfile(language=self.name, package_manager="bundler")
        gemfile = root / "Gemfile"
        if gemfile.exists():
            text = self._read(gemfile)
            seen: set[str] = set()
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped.startswith("gem"):
                    continue
                # gem "rails", "~> 7.0"
                parts = stripped.split('"')
                if len(parts) < 2:
                    parts = stripped.split("'")
                if len(parts) >= 2:
                    name = parts[1].strip()
                    friendly = _RUBY_FRAMEWORK_MAP.get(name)
                    if friendly and friendly not in seen:
                        seen.add(friendly)
                        profile.frameworks.append(
                            Evidence(fact=friendly, source="Gemfile")
                        )
                    if name in {"rspec", "minitest"} and not profile.test_framework:
                        profile.test_framework = name

        for d in ("app", "lib", "config"):
            if (root / d).is_dir():
                profile.source_dirs.append(d)
        for d in ("spec", "test"):
            if (root / d).is_dir():
                profile.test_dirs.append(d)
        return profile
