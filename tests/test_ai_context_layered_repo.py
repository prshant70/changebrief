"""Tests for sub-directory rendering, major-import detection, and architecture inference.

These tests model a Torpedo-style 1mg layout (`app/routes`, `app/managers`,
`app/repositories`, etc.) — exactly the case that prompted these features.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from changebrief.cli import app
from changebrief.core.ai_context.composer import compose_context
from changebrief.core.ai_context.config_loader import load_context_config
from changebrief.core.ai_context.scanner import scan_repo


@pytest.fixture()
def torpedo_repo(tmp_path: Path) -> Path:
    """A 1mg-style repo: app/{routes,managers,repositories,clients,models,...} on Torpedo."""
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "notification_core"',
                'description = "Notification dispatch service for 1mg."',
                "dependencies = [",
                '  "torpedo>=1.0",',
                '  "sanic>=23",',
                '  "sqlalchemy>=2",',
                '  "pydantic>=2",',
                "]",
            ]
        ),
        encoding="utf-8",
    )
    app_dir = tmp_path / "app"
    for sub in (
        "routes", "managers", "repositories", "clients", "models",
        "exceptions", "constants", "schemas", "utils", "services",
    ):
        (app_dir / sub).mkdir(parents=True)
        (app_dir / sub / "__init__.py").write_text(
            "from torpedo.app import App\nimport sanic\n",
            encoding="utf-8",
        )
    (app_dir / "routes" / "notify.py").write_text(
        "from torpedo.app import App\n@App.route('/notify')\nasync def notify(r): ...\n",
        encoding="utf-8",
    )
    (app_dir / "managers" / "notification_manager.py").write_text(
        "from torpedo.base import Base\nclass NotificationManager(Base):\n    pass\n",
        encoding="utf-8",
    )
    (app_dir / "repositories" / "user_repository.py").write_text(
        "from sqlalchemy import select\nimport torpedo\nclass UserRepository: pass\n",
        encoding="utf-8",
    )
    (app_dir / "clients" / "sms_client.py").write_text(
        "from torpedo.client import Client\nclass SMSClient(Client): ...\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    return tmp_path


def test_nested_dirs_under_app_are_listed(torpedo_repo: Path) -> None:
    ctx = scan_repo(torpedo_repo)
    children = {d.name for d in ctx.nested_directories if d.parent_dir == "app"}
    assert {"routes", "managers", "repositories", "clients", "models",
            "schemas", "exceptions", "constants", "utils", "services"} <= children


def test_major_imports_detect_torpedo(torpedo_repo: Path) -> None:
    ctx = scan_repo(torpedo_repo)
    py = next(p for p in ctx.profiles if p.language == "python")
    assert py.major_imports.get("torpedo", 0) >= 5
    # Sanic is curated; it should still show up in declared deps.
    assert "torpedo" in py.declared_dependencies


def test_compose_renders_nested_structure_and_architecture(torpedo_repo: Path) -> None:
    ctx = scan_repo(torpedo_repo)
    cfg = load_context_config(torpedo_repo)
    ai_ctx = compose_context(ctx, cfg)
    titles = [s.title for s in ai_ctx.sections]
    assert "Architecture (inferred from layout)" in titles

    flat = "\n".join(b for s in ai_ctx.sections for b in s.bullets)
    # Sub-dirs surface (no longer hidden one level down).
    assert "app/routes/" in flat
    assert "app/managers/" in flat
    assert "app/repositories/" in flat
    assert "app/clients/" in flat
    # Architecture layers labelled.
    assert "Entry layer" in flat
    assert "Business orchestration" in flat
    assert "Data access" in flat

    # Typical request shape rendered as a paragraph (not a bullet).
    arch = next(s for s in ai_ctx.sections if s.title.startswith("Architecture"))
    paragraphs = "\n".join(arch.paragraphs)
    assert "→" in paragraphs
    assert "app/routes/" in paragraphs


def test_unknown_major_import_surfaces_in_stack(torpedo_repo: Path) -> None:
    ctx = scan_repo(torpedo_repo)
    cfg = load_context_config(torpedo_repo)
    ai_ctx = compose_context(ctx, cfg)
    stack = next(s for s in ai_ctx.sections if s.title == "Stack & dependencies")
    flat = "\n".join(stack.bullets)
    # Without an override, torpedo shows up by name with its import count.
    assert "torpedo" in flat
    assert "observed in" in flat


def test_framework_override_replaces_default_label(torpedo_repo: Path) -> None:
    cfg_dir = torpedo_repo / ".changebrief"
    cfg_dir.mkdir()
    (cfg_dir / "context.yaml").write_text(
        "frameworks:\n"
        '  torpedo: "Torpedo (Sanic-based async framework)"\n',
        encoding="utf-8",
    )
    ctx = scan_repo(torpedo_repo)
    cfg = load_context_config(torpedo_repo)
    ai_ctx = compose_context(ctx, cfg)
    stack = next(s for s in ai_ctx.sections if s.title == "Stack & dependencies")
    flat = "\n".join(stack.bullets)
    assert "Torpedo (Sanic-based async framework)" in flat
    # Evidence trail mentions the import count, not just pyproject.
    assert "imported in" in flat


def test_cli_tip_appears_for_undescribed_majors(torpedo_repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["ai-context", "init", "--dry-run", "--path", str(torpedo_repo), "--targets", "claude"],
    )
    assert result.exit_code == 0, result.output
    assert "torpedo" in result.output
    assert "context.yaml" in result.output


def test_cli_tip_silenced_after_override(torpedo_repo: Path) -> None:
    cfg_dir = torpedo_repo / ".changebrief"
    cfg_dir.mkdir()
    (cfg_dir / "context.yaml").write_text(
        'frameworks:\n  torpedo: "Torpedo"\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["ai-context", "init", "--dry-run", "--path", str(torpedo_repo), "--targets", "claude"],
    )
    assert result.exit_code == 0, result.output
    # The "tip:" CLI nudge should be gone.
    assert "tip: heavily-imported" not in result.output


def test_architecture_omitted_when_no_layered_dirs(tmp_path: Path) -> None:
    """A flat Python repo with no architectural folders gets no architecture section."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "flat"\ndescription = "x"\ndependencies = ["pytest"]\n',
        encoding="utf-8",
    )
    pkg = tmp_path / "flat"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text("def go(): pass\n", encoding="utf-8")
    ctx = scan_repo(tmp_path)
    ai_ctx = compose_context(ctx, load_context_config(tmp_path))
    titles = [s.title for s in ai_ctx.sections]
    assert "Architecture (inferred from layout)" not in titles
