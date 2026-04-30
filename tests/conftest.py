import os
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """
    Force pytest temp directories under the workspace.

    The execution sandbox allows writes only under the workspace directory. On
    macOS, pytest defaults to the OS temp dir (outside workspace), which makes
    `git init` fail when it tries to create `.git/hooks`.
    """
    repo_root = Path(__file__).resolve().parents[1]
    base = repo_root / ".tmp" / "pytest"
    base.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = base


@pytest.fixture(autouse=True, scope="session")
def _isolate_home_and_disable_llm(tmp_path_factory: pytest.TempPathFactory) -> None:
    """
    Ensure tests never make real LLM/network calls and never touch the user's
    real ~/.changebrief cache/config.
    """
    repo_root = Path(__file__).resolve().parents[1]
    home = repo_root / ".tmp" / f"test-home-{os.getpid()}"
    home.mkdir(parents=True, exist_ok=True)

    os.environ["HOME"] = str(home)
    os.environ["CHANGEBRIEF_DISABLE_LLM"] = "1"

