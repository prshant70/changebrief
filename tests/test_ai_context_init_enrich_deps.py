"""Tests for `ai-context init --enrich-deps`.

These tests are hermetic and avoid creating new git repos (sandbox blocks
`.git/hooks` creation in temp dirs). Instead, they reuse the existing
ChangeBrief repo as a `git+file://` dependency pinned to the current HEAD.

LLM calls are disabled by the session fixture in `conftest.py`, so this validates
the deterministic baseline of dependency learning.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from changebrief.cli import app

runner = CliRunner()


def _git(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _framework_repo_and_ref() -> tuple[Path, str, str]:
    """Return (repo_root, head_sha, description) for this repo."""
    repo_root = Path(__file__).resolve().parents[1]
    sha = _git(["git", "rev-parse", "HEAD"], repo_root)
    pp = (repo_root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
    m = re.search(r'^\s*description\s*=\s*["\']([^"\']+)["\']', pp, re.MULTILINE)
    desc = m.group(1).strip() if m else ""
    return repo_root, sha, desc


def _make_consumer_repo(tmp_path: Path, fw_repo: Path, fw_ref: str) -> Path:
    consumer = tmp_path / "consumer"
    consumer.mkdir(parents=True)

    # Use a pinned git+file dependency.
    fw_url = f"git+file://{fw_repo.resolve()}@{fw_ref}"
    (consumer / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "svc"',
                'description = "Service that consumes changebrief."',
                "dependencies = [",
                f'  "changebrief @ {fw_url}",',
                '  "pytest>=8",',
                "]",
            ]
        ),
        encoding="utf-8",
    )

    pkg = consumer / "svc"
    pkg.mkdir()
    # Import the framework in multiple files so it becomes a `major_import`.
    (pkg / "__init__.py").write_text("import changebrief\n", encoding="utf-8")
    (pkg / "a.py").write_text("import changebrief\n", encoding="utf-8")
    (pkg / "b.py").write_text("import changebrief\n", encoding="utf-8")
    (consumer / "tests").mkdir()
    return consumer


def test_init_enrich_deps_learns_framework_description(tmp_path: Path) -> None:
    fw_repo, ref, fw_desc = _framework_repo_and_ref()
    consumer = _make_consumer_repo(tmp_path, fw_repo, ref)

    result = runner.invoke(
        app,
        [
            "ai-context",
            "init",
            "--path",
            str(consumer),
            "--enrich-deps",
            "--targets",
            "claude",
        ],
    )
    assert result.exit_code == 0, result.output

    text = (consumer / "CLAUDE.md").read_text(encoding="utf-8")
    # The learned description from the framework repo should now appear.
    assert fw_desc and fw_desc in text


def test_init_enrich_deps_respects_host_allowlist(tmp_path: Path) -> None:
    fw_repo, ref, _fw_desc = _framework_repo_and_ref()
    consumer = _make_consumer_repo(tmp_path, fw_repo, ref)

    # Disallow file:// host; dependency learning should be skipped.
    result = runner.invoke(
        app,
        [
            "ai-context",
            "init",
            "--path",
            str(consumer),
            "--enrich-deps",
            "--dep-hosts",
            "github.com",
            "--targets",
            "claude",
        ],
    )
    assert result.exit_code == 0, result.output

    text = (consumer / "CLAUDE.md").read_text(encoding="utf-8")
    # The dependency-learning-derived description should *not* appear.
    _, _, fw_desc = _framework_repo_and_ref()
    assert fw_desc and fw_desc not in text


def test_init_enrich_deps_python_requirements_txt(tmp_path: Path) -> None:
    fw_repo, ref, fw_desc = _framework_repo_and_ref()
    consumer = tmp_path / "consumer-req"
    consumer.mkdir(parents=True)
    fw_url = f"git+file://{fw_repo.resolve()}@{ref}#egg=changebrief"
    (consumer / "requirements.txt").write_text(f"{fw_url}\n", encoding="utf-8")
    # minimal python package importing the dep name so it shows up as a major import
    pkg = consumer / "svc"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import changebrief\n", encoding="utf-8")
    (pkg / "a.py").write_text("import changebrief\n", encoding="utf-8")
    (pkg / "b.py").write_text("import changebrief\n", encoding="utf-8")
    (consumer / "tests").mkdir()

    result = runner.invoke(
        app,
        ["ai-context", "init", "--path", str(consumer), "--enrich-deps", "--targets", "claude"],
    )
    assert result.exit_code == 0, result.output
    text = (consumer / "CLAUDE.md").read_text(encoding="utf-8")
    assert fw_desc and fw_desc in text


def test_init_enrich_deps_python_pipfile(tmp_path: Path) -> None:
    fw_repo, ref, fw_desc = _framework_repo_and_ref()
    consumer = tmp_path / "consumer-pipfile"
    consumer.mkdir(parents=True)
    (consumer / "Pipfile").write_text(
        "\n".join(
            [
                "[[source]]",
                'url = "https://pypi.org/simple"',
                'verify_ssl = true',
                'name = "pypi"',
                "",
                "[packages]",
                f'changebrief = {{git = "file://{fw_repo.resolve()}", ref = "{ref}"}}',
            ]
        ),
        encoding="utf-8",
    )
    pkg = consumer / "svc"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import changebrief\n", encoding="utf-8")
    (pkg / "a.py").write_text("import changebrief\n", encoding="utf-8")
    (pkg / "b.py").write_text("import changebrief\n", encoding="utf-8")
    (consumer / "tests").mkdir()

    result = runner.invoke(
        app,
        ["ai-context", "init", "--path", str(consumer), "--enrich-deps", "--targets", "claude"],
    )
    assert result.exit_code == 0, result.output
    text = (consumer / "CLAUDE.md").read_text(encoding="utf-8")
    assert fw_desc and fw_desc in text

