"""Tests for `changebrief ai-context build` and the layered context loader.

The ``build`` command writes to ``~/.changebrief/context.yaml`` (the per-user
overrides). The session-scoped ``conftest._isolate_home_and_disable_llm``
fixture redirects ``HOME`` to a workspace-local temp dir so these tests
never touch the real config.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from changebrief.cli import app
from changebrief.core.ai_context.composer import compose_context
from changebrief.core.ai_context.config_loader import load_context_config
from changebrief.core.ai_context.scanner import scan_repo

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_home_context() -> None:
    """Wipe ~/.changebrief/context.yaml before *and* after each test.

    The session-scoped HOME fixture in conftest is shared across tests; without
    this guard, build-tests would leak framework entries into unrelated tests
    that don't expect a populated home config.
    """
    home_yaml = Path.home() / ".changebrief" / "context.yaml"
    if home_yaml.exists():
        home_yaml.unlink()
    yield
    if home_yaml.exists():
        home_yaml.unlink()


def _make_torpedo_repo(tmp_path: Path) -> Path:
    """A repo that *defines* a custom framework (not a consumer of one)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "torpedo"',
                'description = "Torpedo - async microservice chassis built on Sanic."',
                "dependencies = [",
                '  "sanic>=23",',
                '  "pydantic>=2",',
                "]",
            ]
        ),
        encoding="utf-8",
    )
    pkg = tmp_path / "torpedo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .app import App\n", encoding="utf-8")
    (pkg / "app.py").write_text(
        "from sanic import Sanic\nclass App: ...\n",
        encoding="utf-8",
    )
    return tmp_path


def _make_consumer_repo(tmp_path: Path) -> Path:
    """A consumer repo: imports `torpedo` but doesn't define it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "notification_core"',
                'description = "Notification dispatch service."',
                "dependencies = [",
                '  "torpedo>=1.0",',
                '  "sqlalchemy>=2",',
                "]",
            ]
        ),
        encoding="utf-8",
    )
    app_dir = tmp_path / "app"
    for sub in ("routes", "managers", "repositories", "clients"):
        (app_dir / sub).mkdir(parents=True)
        (app_dir / sub / "__init__.py").write_text(
            "from torpedo.app import App\n", encoding="utf-8"
        )
    (app_dir / "routes" / "notify.py").write_text(
        "from torpedo.app import App\n@App.route('/notify')\nasync def notify(r): ...\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    return tmp_path


def _home_context_yaml() -> Path:
    return Path.home() / ".changebrief" / "context.yaml"


def _reset_home_context() -> None:
    p = _home_context_yaml()
    if p.exists():
        p.unlink()


def test_build_writes_entry_to_home_context(tmp_path: Path) -> None:
    _reset_home_context()
    framework_repo = _make_torpedo_repo(tmp_path)

    result = runner.invoke(app, ["ai-context", "build", "--path", str(framework_repo)])
    assert result.exit_code == 0, result.output
    assert "added `torpedo`" in result.output

    saved = yaml.safe_load(_home_context_yaml().read_text(encoding="utf-8"))
    assert "frameworks" in saved
    assert "torpedo" in saved["frameworks"]
    desc = saved["frameworks"]["torpedo"]
    assert "Torpedo" in desc
    assert "Sanic" in desc


def test_build_dry_run_does_not_write(tmp_path: Path) -> None:
    _reset_home_context()
    framework_repo = _make_torpedo_repo(tmp_path)

    result = runner.invoke(
        app, ["ai-context", "build", "--path", str(framework_repo), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "torpedo" in result.output
    assert not _home_context_yaml().exists(), "dry-run must not touch disk"


def test_build_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    _reset_home_context()
    framework_repo = _make_torpedo_repo(tmp_path)

    first = runner.invoke(app, ["ai-context", "build", "--path", str(framework_repo)])
    assert first.exit_code == 0, first.output

    second = runner.invoke(
        app,
        [
            "ai-context",
            "build",
            "--path",
            str(framework_repo),
            "--description",
            "Brand new description",
        ],
    )
    # Conflict-without-force is reported as a validation error.
    assert second.exit_code == 2, second.output
    assert "Refusing to overwrite" in second.output

    saved = yaml.safe_load(_home_context_yaml().read_text(encoding="utf-8"))
    assert "Torpedo" in saved["frameworks"]["torpedo"]
    assert "Brand new description" not in saved["frameworks"]["torpedo"]


def test_build_force_overwrites(tmp_path: Path) -> None:
    _reset_home_context()
    framework_repo = _make_torpedo_repo(tmp_path)

    runner.invoke(app, ["ai-context", "build", "--path", str(framework_repo)])
    second = runner.invoke(
        app,
        [
            "ai-context",
            "build",
            "--path",
            str(framework_repo),
            "--description",
            "Curated by ops team.",
            "--force",
        ],
    )
    assert second.exit_code == 0, second.output

    saved = yaml.safe_load(_home_context_yaml().read_text(encoding="utf-8"))
    assert saved["frameworks"]["torpedo"] == "Curated by ops team."


def test_build_overrides_and_notes_persist(tmp_path: Path) -> None:
    _reset_home_context()
    framework_repo = _make_torpedo_repo(tmp_path)

    result = runner.invoke(
        app,
        [
            "ai-context",
            "build",
            "--path",
            str(framework_repo),
            "--name",
            "torpedo-core",
            "--description",
            "Torpedo (curated)",
            "--note",
            "Use `from torpedo import send_response` for responses.",
            "--note",
            "Don't `print()` in handlers.",
        ],
    )
    assert result.exit_code == 0, result.output

    saved = yaml.safe_load(_home_context_yaml().read_text(encoding="utf-8"))
    assert saved["frameworks"]["torpedo-core"] == "Torpedo (curated)"
    assert "Use `from torpedo import send_response` for responses." in saved["notes"]
    assert "Don't `print()` in handlers." in saved["notes"]


def test_build_then_init_uses_home_entry(tmp_path: Path) -> None:
    """The whole point of ``build``: a later ``init`` should pick up the entry."""
    _reset_home_context()
    framework_repo = _make_torpedo_repo(tmp_path / "torpedo_src")
    runner.invoke(
        app,
        [
            "ai-context",
            "build",
            "--path",
            str(framework_repo),
            "--description",
            "Torpedo (Sanic-based async framework).",
            "--force",
        ],
    )

    consumer_repo = _make_consumer_repo(tmp_path / "consumer")
    result = runner.invoke(
        app,
        ["ai-context", "init", "--path", str(consumer_repo), "--targets", "claude"],
    )
    assert result.exit_code == 0, result.output

    text = (consumer_repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Torpedo (Sanic-based async framework)" in text


def test_loader_layers_home_and_repo(tmp_path: Path) -> None:
    """Per-repo file extends per-user file rather than fully shadowing it."""
    _reset_home_context()
    home_path = _home_context_yaml()
    home_path.parent.mkdir(parents=True, exist_ok=True)
    home_path.write_text(
        "\n".join(
            [
                "frameworks:",
                '  torpedo: "Torpedo (org default)"',
                "do:",
                '  - "Always run pytest before pushing."',
            ]
        ),
        encoding="utf-8",
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg_dir = repo / ".changebrief"
    cfg_dir.mkdir()
    (cfg_dir / "context.yaml").write_text(
        "\n".join(
            [
                'project_summary: "Repo-specific summary"',
                "do:",
                '  - "Use the @handle_errors decorator."',
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_context_config(repo)
    assert cfg.project_summary == "Repo-specific summary"
    assert cfg.frameworks.get("torpedo") == "Torpedo (org default)"
    assert "Always run pytest before pushing." in cfg.do
    assert "Use the @handle_errors decorator." in cfg.do


def test_loader_repo_frameworks_override_home(tmp_path: Path) -> None:
    _reset_home_context()
    home_path = _home_context_yaml()
    home_path.parent.mkdir(parents=True, exist_ok=True)
    home_path.write_text(
        'frameworks:\n  torpedo: "Torpedo (org default)"\n',
        encoding="utf-8",
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".changebrief").mkdir()
    (repo / ".changebrief" / "context.yaml").write_text(
        'frameworks:\n  torpedo: "Torpedo (repo override)"\n',
        encoding="utf-8",
    )

    cfg = load_context_config(repo)
    assert cfg.frameworks["torpedo"] == "Torpedo (repo override)"


def test_compose_uses_layered_home_framework(tmp_path: Path) -> None:
    """End-to-end: after ``build`` writes the home entry, ``compose_context`` renders it."""
    _reset_home_context()
    home_path = _home_context_yaml()
    home_path.parent.mkdir(parents=True, exist_ok=True)
    home_path.write_text(
        'frameworks:\n  torpedo: "Torpedo (Sanic-based)"\n',
        encoding="utf-8",
    )
    consumer = _make_consumer_repo(tmp_path)

    repo_ctx = scan_repo(consumer)
    cfg = load_context_config(consumer)
    ai_ctx = compose_context(repo_ctx, cfg)

    stack = next(s for s in ai_ctx.sections if s.title == "Stack & dependencies")
    assert any("Torpedo (Sanic-based)" in b for b in stack.bullets)
