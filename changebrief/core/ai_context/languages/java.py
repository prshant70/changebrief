"""Java / Kotlin (JVM) adapter."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from changebrief.core.ai_context.languages.base import LanguageAdapter
from changebrief.core.ai_context.models import Evidence, LanguageProfile


_JVM_FRAMEWORK_MAP = {
    "spring-boot-starter": "Spring Boot",
    "spring-boot-starter-web": "Spring Boot (web)",
    "spring-webflux": "Spring WebFlux",
    "micronaut": "Micronaut",
    "quarkus": "Quarkus",
    "vert.x": "Vert.x",
    "dropwizard": "Dropwizard",
    "junit": "JUnit",
    "junit-jupiter-api": "JUnit Jupiter",
    "kotest": "Kotest",
    "mockito": "Mockito",
    "ktor": "Ktor",
    "hibernate": "Hibernate",
    "grpc": "gRPC",
}


class JavaAdapter(LanguageAdapter):
    name = "java"
    file_extensions = (".java", ".kt", ".kts")
    config_files = ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle")

    def detect(self, root: Path, files_by_ext: Dict[str, int]) -> bool:
        return any(files_by_ext.get(e, 0) > 0 for e in self.file_extensions) or any(
            (root / cf).exists() for cf in self.config_files
        )

    def gather(self, root: Path) -> LanguageProfile:
        profile = LanguageProfile(language=self.name)

        pom = root / "pom.xml"
        if pom.exists():
            profile.package_manager = "Maven"
            text = self._read(pom)
            for fragment, friendly in _JVM_FRAMEWORK_MAP.items():
                if fragment in text:
                    profile.frameworks.append(Evidence(fact=friendly, source="pom.xml"))

        for gradle_name in ("build.gradle", "build.gradle.kts"):
            gradle = root / gradle_name
            if gradle.exists():
                profile.package_manager = profile.package_manager or "Gradle"
                text = self._read(gradle)
                for fragment, friendly in _JVM_FRAMEWORK_MAP.items():
                    if fragment in text:
                        profile.frameworks.append(
                            Evidence(fact=friendly, source=gradle_name)
                        )
                break

        # Standard Maven/Gradle layout.
        for d in (
            "src/main/java",
            "src/main/kotlin",
            "src/main/resources",
        ):
            if (root / d).is_dir():
                profile.source_dirs.append(d)
        for d in ("src/test/java", "src/test/kotlin"):
            if (root / d).is_dir():
                profile.test_dirs.append(d)

        return profile
