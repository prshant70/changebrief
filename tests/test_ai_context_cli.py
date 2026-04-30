"""End-to-end CLI smoke tests for `changebrief ai-context`."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from changebrief.cli import app

runner = CliRunner()


def _make_py_repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '\n'.join(
            [
                "[project]",
                'name = "demo"',
                'description = "Demo for CLI test."',
                "dependencies = [",
                '  "fastapi>=0.100",',
                '  "pytest>=8",',
                '  "ruff>=0.4",',
                '  "mypy>=1.10",',
                "]",
            ]
        ),
        encoding="utf-8",
    )
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/x')\ndef x(): return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    return tmp_path


def test_ai_context_init_writes_three_files(tmp_path: Path) -> None:
    repo = _make_py_repo(tmp_path)
    result = runner.invoke(app, ["ai-context", "init", "--path", str(repo)])
    assert result.exit_code == 0, result.output
    for name in ("CLAUDE.md", "CURSOR.md", "CODEX.md"):
        path = repo / name
        assert path.exists(), f"{name} should be written"
        text = path.read_text(encoding="utf-8")
        assert "FastAPI" in text
        assert "changebrief:ai-context:start" in text
        assert "## Local development" in text
        # Local-dev should be richer than just tests when tooling deps exist.
        assert "`lint`" in text
        assert "`typecheck`" in text


def test_ai_context_init_dry_run_does_not_write(tmp_path: Path) -> None:
    repo = _make_py_repo(tmp_path)
    result = runner.invoke(app, ["ai-context", "init", "--path", str(repo), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "FastAPI" in result.output
    assert not (repo / "CLAUDE.md").exists()


def test_ai_context_init_refuses_to_overwrite_unmarked_file(tmp_path: Path) -> None:
    repo = _make_py_repo(tmp_path)
    (repo / "CLAUDE.md").write_text("hand-written, do not touch", encoding="utf-8")
    result = runner.invoke(app, ["ai-context", "init", "--path", str(repo), "--targets", "claude"])
    assert result.exit_code == 0, result.output
    # File untouched.
    assert (repo / "CLAUDE.md").read_text(encoding="utf-8") == "hand-written, do not touch"


def test_ai_context_init_force_overwrites_unmarked(tmp_path: Path) -> None:
    repo = _make_py_repo(tmp_path)
    (repo / "CLAUDE.md").write_text("hand-written", encoding="utf-8")
    result = runner.invoke(
        app,
        ["ai-context", "init", "--path", str(repo), "--targets", "claude", "--force"],
    )
    assert result.exit_code == 0, result.output
    text = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "FastAPI" in text


def test_ai_context_init_unknown_target_errors(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["ai-context", "init", "--path", str(tmp_path), "--targets", "bogus"],
    )
    assert result.exit_code == 2  # typer.BadParameter -> usage error


def test_ai_context_init_uses_config_overrides(tmp_path: Path) -> None:
    repo = _make_py_repo(tmp_path)
    cfg_dir = repo / ".changebrief"
    cfg_dir.mkdir()
    (cfg_dir / "context.yaml").write_text(
        '\n'.join(
            [
                'project_summary: "demo (custom summary)"',
                "do:",
                '  - "Use the @handle_errors decorator on new commands."',
                "dont:",
                '  - "Do not log API keys."',
                "notes:",
                '  - "Open a PR with `gh pr create`."',
            ]
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["ai-context", "init", "--path", str(repo), "--targets", "claude"])
    assert result.exit_code == 0, result.output
    text = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "demo (custom summary)" in text
    assert "@handle_errors" in text
    assert "Do not log API keys" in text
    assert "Open a PR with `gh pr create`" in text


def test_ai_context_init_pipfile_repo_infers_tooling_scripts(tmp_path: Path) -> None:
    repo = tmp_path / "pipfile-demo"
    repo.mkdir()
    # Pipenv marker for package manager detection.
    (repo / "Pipfile.lock").write_text("{}", encoding="utf-8")
    (repo / "Pipfile").write_text(
        "\n".join(
            [
                "[packages]",
                'fastapi = "*"',
                "",
                "[dev-packages]",
                'pytest = "*"',
                'ruff = "*"',
                'mypy = "*"',
                'pre-commit = "*"',
            ]
        ),
        encoding="utf-8",
    )
    pkg = repo / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests").mkdir()

    result = runner.invoke(app, ["ai-context", "init", "--path", str(repo), "--targets", "claude"])
    assert result.exit_code == 0, result.output
    text = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    # Local-dev scripts should use pipenv prefix.
    assert "`lint`" in text and "pipenv run ruff check ." in text
    assert "`typecheck`" in text and "pipenv run mypy ." in text
    assert "`precommit`" in text and "pipenv run pre-commit run --all-files" in text


def test_ai_context_init_inferrs_tooling_from_config_files(tmp_path: Path) -> None:
    repo = tmp_path / "cfg-demo"
    repo.mkdir()
    (repo / "Pipfile.lock").write_text("{}", encoding="utf-8")
    # No dev dependencies declared, but config files exist.
    (repo / "Pipfile").write_text(
        "\n".join(["[packages]", 'fastapi = "*"']), encoding="utf-8"
    )
    (repo / "ruff.toml").write_text("[lint]\n", encoding="utf-8")
    (repo / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    pkg = repo / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests").mkdir()

    result = runner.invoke(app, ["ai-context", "init", "--path", str(repo), "--targets", "claude"])
    assert result.exit_code == 0, result.output
    text = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "pipenv run ruff check ." in text
    assert "pipenv run mypy ." in text
    assert "pipenv run pre-commit run --all-files" in text


def test_ai_context_init_extracts_run_commands_from_readme(tmp_path: Path) -> None:
    repo = tmp_path / "readme-demo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname="demo"\ndescription="x"\ndependencies=["pytest"]\n',
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "\n".join(
            [
                "## Setup",
                "```",
                "pip install -r requirements/base.txt",
                "python3 -m app.service",
                "```",
            ]
        ),
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    pkg = repo / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")

    result = runner.invoke(app, ["ai-context", "init", "--path", str(repo), "--targets", "claude"])
    assert result.exit_code == 0, result.output
    text = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "`install`" in text and "pip install -r requirements/base.txt" in text
    assert "`run`" in text and "python3 -m app.service" in text
