"""Tests for the ai-context scanner + adapters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from changebrief.core.ai_context.composer import compose_context
from changebrief.core.ai_context.config_loader import load_context_config
from changebrief.core.ai_context.scanner import scan_repo


@pytest.fixture()
def py_repo(tmp_path: Path) -> Path:
    """A small Python repo with FastAPI + pytest signals."""
    (tmp_path / "pyproject.toml").write_text(
        '\n'.join(
            [
                "[project]",
                'name = "demo"',
                'description = "Demo service for tests."',
                "dependencies = [",
                '  "fastapi>=0.100",',
                '  "pydantic>=2",',
                '  "openai>=1",',
                "]",
                "",
                "[project.optional-dependencies]",
                'dev = ["pytest>=8"]',
                "",
                "[project.scripts]",
                'demo-server = "demo.main:run"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    src = tmp_path / "demo"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "main.py").write_text(
        '\n'.join(
            [
                "from fastapi import FastAPI",
                "app = FastAPI()",
                "@app.get('/health')",
                "def health(): return {'ok': True}",
                "",
                "class UserService:",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_health.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("name: ci\n")
    return tmp_path


def test_scan_python_repo_extracts_evidence(py_repo: Path) -> None:
    ctx = scan_repo(py_repo)
    assert ctx.primary_language == "python"
    assert ctx.project_name == "demo"
    assert ctx.project_summary == "Demo service for tests."
    assert ctx.has_readme is True
    assert ctx.license_name == "MIT"
    assert "GitHub Actions" in ctx.has_ci

    py_profile = next(p for p in ctx.profiles if p.language == "python")
    fw_facts = {e.fact for e in py_profile.frameworks}
    assert "FastAPI" in fw_facts
    assert "Pydantic" in fw_facts
    assert "OpenAI SDK" in fw_facts
    # Evidence is path-anchored, not invented.
    assert all(e.source.startswith("pyproject.toml") for e in py_profile.frameworks)
    assert py_profile.test_framework == "pytest"
    assert "demo" in py_profile.source_dirs
    assert "tests" in py_profile.test_dirs
    assert any("/health" in ep for ep in py_profile.entry_points)
    assert "demo-server" in py_profile.run_scripts


def test_scan_node_repo(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "node-demo",
                "description": "demo",
                "scripts": {"test": "jest", "build": "tsc"},
                "dependencies": {"express": "^4", "openai": "^4"},
                "devDependencies": {"jest": "^29", "typescript": "^5"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text(
        "import express from 'express'; const app = express(); app.get('/x', (req,res)=>res.send('ok'));\n",
        encoding="utf-8",
    )
    ctx = scan_repo(tmp_path)
    assert ctx.primary_language == "typescript"
    ts_profile = next(p for p in ctx.profiles if p.language == "typescript")
    fw_names = {e.fact for e in ts_profile.frameworks}
    assert "Express" in fw_names
    assert "OpenAI SDK" in fw_names
    assert ts_profile.test_framework == "jest"
    assert ts_profile.run_scripts.get("test") == "jest"


def test_scan_go_repo(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text(
        "module example.com/demo\ngo 1.22\nrequire github.com/gin-gonic/gin v1.9.0\n",
        encoding="utf-8",
    )
    (tmp_path / "main.go").write_text("package main\n", encoding="utf-8")
    ctx = scan_repo(tmp_path)
    assert ctx.primary_language == "go"
    go_profile = next(p for p in ctx.profiles if p.language == "go")
    assert any(e.fact == "Gin" for e in go_profile.frameworks)


def test_scan_unknown_repo_falls_back_to_generic(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\techo hi\nbuild:\n\techo build\n", encoding="utf-8")
    ctx = scan_repo(tmp_path)
    assert ctx.primary_language is None  # No real language detected.
    # Generic adapter should have surfaced make targets.
    generic = next((p for p in ctx.profiles if p.language == "generic"), None)
    assert generic is not None
    assert "test" in generic.run_scripts
    assert generic.run_scripts["test"] == "make test"


def test_compose_only_uses_evidence_backed_facts(py_repo: Path) -> None:
    ctx = scan_repo(py_repo)
    cfg = load_context_config(py_repo)
    ai = compose_context(ctx, cfg)
    rendered = "\n".join(b for s in ai.sections for b in s.bullets)
    # Frameworks must show up with evidence breadcrumb.
    assert "FastAPI" in rendered
    assert "evidence: `pyproject.toml" in rendered
    # No generic "X framework" placeholders.
    assert "X framework" not in rendered
