"""Smoke tests for the ChangeBrief CLI."""

import logging
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from changebrief.cli import app

runner = CliRunner()


def _exit_code_main(*args: str) -> int:
    """Run the real console entrypoint (same as the `changebrief` script)."""
    old = sys.argv[:]
    try:
        sys.argv = ["changebrief", *args]
        from changebrief.cli import main

        try:
            main()
        except SystemExit as exc:
            return int(exc.code) if exc.code is not None else 0
        return 0
    finally:
        sys.argv = old


def _git_init_with_commit(repo: Path) -> None:
    try:
        subprocess.run(
            ["git", "init"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        # In some sandboxed environments, creating `.git/*` is disallowed.
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
        if "Operation not permitted" in stderr:
            pytest.skip(f"git init not permitted in this environment: {stderr.strip()}")
        raise
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_root_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ChangeBrief" in result.stdout
    assert "validate" in result.stdout
    assert "config" in result.stdout
    assert "cache" in result.stdout


def test_validate_requires_args() -> None:
    result = runner.invoke(app, ["validate"])
    assert result.exit_code != 0


def test_cache_list_and_purge(tmp_path: Path) -> None:
    # Create a fake cache file under isolated HOME (set by tests/conftest.py).
    from changebrief.core.cache.store import get_cache_root

    root = get_cache_root()
    d = root / "repo123" / "aaaa..bbbb" / "v1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "change_summary.json").write_text(
        '{"created_at": 1, "ttl_s": 1, "key": "change_summary", "value": {"x": 1}}\n',
        encoding="utf-8",
    )

    res = runner.invoke(app, ["cache", "list"])
    assert res.exit_code == 0
    assert "repo123" in res.stdout

    res2 = runner.invoke(app, ["cache", "purge", "--all"])
    assert res2.exit_code == 0
    assert "Deleted" in res2.stdout


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "changebrief" in result.stdout


def test_config_check_ok(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "llm_provider: openai\n"
        "llm_api_key: sk-test\n"
        "default_model: gpt-4o-mini\n"
        "log_level: INFO\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["config", "check", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "Configuration is valid" in result.stdout
    assert "llm_provider" in result.stdout
    assert "openai" in result.stdout


def test_config_check_warns_empty_api_key(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "llm_provider: openai\n"
        "llm_api_key: ''\n"
        "default_model: gpt-4o-mini\n"
        "log_level: INFO\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["config", "check", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "llm_api_key is empty" in (result.stdout + result.stderr)


def test_config_check_invalid_log_level(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "llm_provider: openai\n"
        "llm_api_key: x\n"
        "default_model: gpt-4o-mini\n"
        "log_level: BOGUS\n",
        encoding="utf-8",
    )
    code = _exit_code_main("config", "check", "--config", str(cfg))
    assert code == 3


def test_config_check_missing_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    code = _exit_code_main("config", "check")
    assert code == 3


def test_validate_rejects_non_git_directory(tmp_path: Path) -> None:
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    code = _exit_code_main(
        "validate",
        "--base",
        "main",
        "--feature",
        "feature",
        "--path",
        str(plain),
    )
    assert code == 2


def test_validate_git_repo(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    _git_init_with_commit(tmp_path)
    subprocess.run(
        ["git", "checkout", "-b", "feature"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    # Make a change on feature.
    (tmp_path / "x.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "change"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    val = runner.invoke(
        app,
        ["validate", "--base", "main", "--feature", "feature", "--path", str(tmp_path)],
    )
    assert val.exit_code == 0
    assert "Analyzing changes" in caplog.text


def test_validate_run_flags_invalid_json(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # Active validation mode removed; keep a basic validate smoke test.
    caplog.set_level(logging.INFO)
    _git_init_with_commit(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "config_template.json").write_text("{bad json", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "bad json"], cwd=tmp_path, check=True, capture_output=True)
    result = runner.invoke(
        app,
        ["validate", "--base", "main", "--feature", "feature", "--path", str(tmp_path)],
    )
    assert result.exit_code == 0


def test_validate_uses_cache_on_repeat(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    _git_init_with_commit(tmp_path)
    subprocess.run(
        ["git", "checkout", "-b", "feature"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "x.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "change"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    caplog.clear()
    r1 = runner.invoke(app, ["validate", "--base", "main", "--feature", "feature", "--path", str(tmp_path)])
    assert r1.exit_code == 0
    assert "Cache miss: change_summary" in caplog.text

    caplog.clear()
    r2 = runner.invoke(app, ["validate", "--base", "main", "--feature", "feature", "--path", str(tmp_path)])
    assert r2.exit_code == 0
    assert "Cache hit: change_summary" in caplog.text


def test_validate_nocache_bypasses_cache(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    _git_init_with_commit(tmp_path)
    subprocess.run(
        ["git", "checkout", "-b", "feature"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "x.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "change"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Prime the cache.
    caplog.clear()
    r1 = runner.invoke(app, ["validate", "--base", "main", "--feature", "feature", "--path", str(tmp_path)])
    assert r1.exit_code == 0
    assert "Cache miss: change_summary" in caplog.text

    # Now bypass it.
    caplog.clear()
    r2 = runner.invoke(
        app,
        ["validate", "--base", "main", "--feature", "feature", "--path", str(tmp_path), "--nocache"],
    )
    assert r2.exit_code == 0
    assert "Cache miss: change_summary" in caplog.text


