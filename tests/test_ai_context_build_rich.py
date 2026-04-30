"""Tests for the deterministic build_extractor and the rich build pipeline.

LLM is always disabled in tests (see ``conftest._isolate_home_and_disable_llm``)
so these exercise the AST-driven baseline that ships in every output.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from changebrief.cli import app
from changebrief.core.ai_context.build_extractor import extract_framework
from changebrief.core.ai_context.builder import build_framework_entry
from changebrief.core.ai_context.scanner import scan_repo

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_home_context() -> None:
    home_yaml = Path.home() / ".changebrief" / "context.yaml"
    if home_yaml.exists():
        home_yaml.unlink()
    yield
    if home_yaml.exists():
        home_yaml.unlink()


@pytest.fixture()
def torpedo_like_repo(tmp_path: Path) -> Path:
    """A small Torpedo-shaped framework repo with __all__, exceptions, and an example."""
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "torpedo"',
                'description = "Torpedo - async microservice chassis built on Sanic."',
                'requires-python = ">=3.12,<4.0"',
                "dependencies = [",
                '  "sanic>=23",',
                '  "aiohttp>=3.9",',
                "]",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "# Torpedo\n\nMicroservice chassis built on Sanic. Wraps `Sanic` with `Torpedo(blueprint, service_config=...)`.\n",
        encoding="utf-8",
    )
    (tmp_path / "config.json").write_text(
        '{"NAME": "demo", "HOST": "0.0.0.0", "PORT": 8000, "SENTRY": {}}',
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text(
        "# Index\n\nConfig keys: NAME, HOST, PORT.\n",
        encoding="utf-8",
    )
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "playground.py").write_text(
        "from torpedo import Torpedo\nfrom sanic import Blueprint\nbp = Blueprint('bp')\n@bp.get('/x')\nasync def x(r): ...\nTorpedo(bp).create_app()\n",
        encoding="utf-8",
    )

    pkg = tmp_path / "torpedo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "\n".join(
            [
                'from .app import Torpedo',
                'from .response import send_response, send_error_response',
                'from .api_clients.clients import BaseAPIClient',
                'from .exceptions import BaseTorpedoException, BadRequestException',
                'from .circuit_breaker import circuit_breaker',
                'CONFIG = {}',
                '__all__ = [',
                '    "Torpedo",',
                '    "send_response",',
                '    "send_error_response",',
                '    "BaseAPIClient",',
                '    "BaseTorpedoException",',
                '    "BadRequestException",',
                '    "circuit_breaker",',
                '    "CONFIG",',
                ']',
            ]
        ),
        encoding="utf-8",
    )
    (pkg / "app.py").write_text(
        "from sanic import Sanic\nclass Torpedo:\n    def __init__(self, blueprint, service_config=None): ...\n    def create_app(self): ...\n",
        encoding="utf-8",
    )
    (pkg / "response.py").write_text(
        "def send_response(data): ...\ndef send_error_response(error): ...\n",
        encoding="utf-8",
    )
    (pkg / "exceptions.py").write_text(
        "\n".join(
            [
                'class BaseTorpedoException(Exception):',
                '    status_code = 500',
                '    sentry_raise = True',
                '',
                'class BadRequestException(BaseTorpedoException):',
                '    status_code = 400',
                '',
                'class HTTPRequestException(BaseTorpedoException):',
                '    status_code = 502',
                '',
                'class InterServiceRequestException(HTTPRequestException):',
                '    pass',
            ]
        ),
        encoding="utf-8",
    )
    api_clients = pkg / "api_clients"
    api_clients.mkdir()
    (api_clients / "__init__.py").write_text("", encoding="utf-8")
    (api_clients / "clients.py").write_text(
        "import aiohttp\nclass BaseAPIClient:\n    _host = None\n    _timeout = None\n    __interservice__ = False\n",
        encoding="utf-8",
    )
    cb = pkg / "circuit_breaker"
    cb.mkdir()
    (cb / "__init__.py").write_text(
        "def circuit_breaker(*a, **k):\n    def deco(fn):\n        return fn\n    return deco\n",
        encoding="utf-8",
    )
    excs_dir = pkg / "exceptions_dir_unused"  # not the actual dir, exceptions.py is the file
    return tmp_path


def test_extractor_finds_public_api_from_dunder_all(torpedo_like_repo: Path) -> None:
    repo_ctx = scan_repo(torpedo_like_repo)
    extraction = extract_framework(repo_ctx, package_name="torpedo")

    names = [s.name for s in extraction.public_api]
    assert "Torpedo" in names
    assert "send_response" in names
    assert "BaseAPIClient" in names
    assert "BaseTorpedoException" in names
    assert "circuit_breaker" in names
    assert "CONFIG" in names

    init_sources = {s.source for s in extraction.public_api}
    # The extractor should have resolved at least some symbols to the package init.
    assert any("torpedo/__init__.py" in s for s in init_sources)


def test_extractor_finds_exception_family(torpedo_like_repo: Path) -> None:
    repo_ctx = scan_repo(torpedo_like_repo)
    extraction = extract_framework(repo_ctx, package_name="torpedo")

    names = {e.name for e in extraction.exceptions}
    assert "BaseTorpedoException" in names
    assert "BadRequestException" in names
    assert "HTTPRequestException" in names
    assert "InterServiceRequestException" in names
    # Each exception is anchored to a real source line.
    for exc in extraction.exceptions:
        assert ":" in exc.source
        rel, _, _ = exc.source.partition(":")
        assert (torpedo_like_repo / rel).is_file()


def test_extractor_picks_up_notable_dirs_examples_and_config(torpedo_like_repo: Path) -> None:
    repo_ctx = scan_repo(torpedo_like_repo)
    extraction = extract_framework(repo_ctx, package_name="torpedo")

    notable_names = {nd.name for nd in extraction.notable_dirs}
    assert "api_clients" in notable_names
    assert "circuit_breaker" in notable_names

    assert any(ex.rel_path.startswith("examples/") for ex in extraction.examples)
    assert "NAME" in extraction.config_keys
    assert "PORT" in extraction.config_keys
    assert extraction.python_version_pin is not None
    assert ">=3.12" in extraction.python_version_pin


def test_extractor_collects_sample_paths_for_citation_verification(torpedo_like_repo: Path) -> None:
    repo_ctx = scan_repo(torpedo_like_repo)
    extraction = extract_framework(repo_ctx, package_name="torpedo")

    sample = set(extraction.sample_paths)
    assert "torpedo/__init__.py" in sample
    assert "torpedo/exceptions.py" in sample
    assert "README.md" in sample
    assert "pyproject.toml" in sample
    assert "config.json" in sample
    # docs and examples surface as cite-eligible paths.
    assert any(p.startswith("docs/") for p in sample)
    assert any(p.startswith("examples/") for p in sample)


def test_build_with_llm_disabled_still_writes_rich_notes(torpedo_like_repo: Path) -> None:
    """Without an LLM the entry is still useful: API surface, exceptions, version pin, config keys."""
    result = runner.invoke(
        app, ["ai-context", "build", "--path", str(torpedo_like_repo)]
    )
    assert result.exit_code == 0, result.output

    saved = yaml.safe_load(
        (Path.home() / ".changebrief" / "context.yaml").read_text(encoding="utf-8")
    )
    assert saved["frameworks"]["torpedo"], "framework description must be set"
    notes = saved["notes"]

    joined = "\n".join(notes)
    assert "Public API" in joined
    assert "Torpedo" in joined and "BaseAPIClient" in joined
    assert "Exception family" in joined
    assert "BaseTorpedoException" in joined
    assert "Python version" in joined and ">=3.12" in joined
    assert "Config keys" in joined and "NAME" in joined
    assert "playground.py" in joined  # smallest end-to-end example


def test_build_cli_prints_extraction_stats(torpedo_like_repo: Path) -> None:
    result = runner.invoke(
        app, ["ai-context", "build", "--path", str(torpedo_like_repo), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "package source" in out and "torpedo" in out
    assert "public API:" in out
    assert "exceptions:" in out
    assert "python pin:" in out
    # LLM is disabled in tests; surface the reason instead of pretending it ran.
    assert "enrichment skipped" in out


def test_build_no_enrich_flag_silences_skip_warning(torpedo_like_repo: Path) -> None:
    result = runner.invoke(
        app,
        [
            "ai-context",
            "build",
            "--path",
            str(torpedo_like_repo),
            "--no-enrich",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    # With --no-enrich, the LLM is intentionally off; we shouldn't warn.
    assert "enrichment skipped" not in result.output


def test_build_then_init_renders_baseline_notes(tmp_path: Path) -> None:
    """End-to-end: build the framework entry, then init in a consumer repo."""
    framework_repo = tmp_path / "torpedo_src"
    # Same fixture, but materialised manually here.
    _ = _make_torpedo_min(framework_repo)

    runner.invoke(app, ["ai-context", "build", "--path", str(framework_repo), "--force"])

    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "pyproject.toml").write_text(
        '[project]\nname = "svc"\ndescription = "X"\ndependencies = ["torpedo"]\n',
        encoding="utf-8",
    )
    pkg = consumer / "svc"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import torpedo\n", encoding="utf-8")
    (pkg / "main.py").write_text("from torpedo import Torpedo\n", encoding="utf-8")
    (consumer / "tests").mkdir()

    result = runner.invoke(
        app,
        ["ai-context", "init", "--path", str(consumer), "--targets", "claude"],
    )
    assert result.exit_code == 0, result.output
    text = (consumer / "CLAUDE.md").read_text(encoding="utf-8")
    # Description from the home-level context.yaml landed in the consumer's CLAUDE.md.
    assert "Torpedo" in text
    # Notes propagate too.
    assert "Public API" in text or "BaseTorpedoException" in text


def _make_torpedo_min(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "torpedo"\ndescription = "Torpedo (mini)"\nrequires-python = ">=3.12"\ndependencies = ["sanic>=23"]\n',
        encoding="utf-8",
    )
    pkg = root / "torpedo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        'from .app import Torpedo\n__all__ = ["Torpedo"]\n', encoding="utf-8"
    )
    (pkg / "app.py").write_text("class Torpedo: ...\n", encoding="utf-8")
    (pkg / "exceptions.py").write_text(
        "class BaseTorpedoException(Exception): ...\n", encoding="utf-8"
    )
    return root


def test_builder_keeps_user_overrides_winning(tmp_path: Path) -> None:
    """``--description`` and ``--note`` always beat extractor / LLM output."""
    repo = _make_torpedo_min(tmp_path / "torp")
    repo_ctx = scan_repo(repo)
    report = build_framework_entry(
        repo_ctx,
        name_override="torpedo",
        description_override="Hand-curated summary.",
        user_notes=["Custom note appended at the end."],
        config={},
        llm_enabled=False,
    )
    assert report.entry.description == "Hand-curated summary."
    assert "Custom note appended at the end." in report.entry.notes
    # Baseline notes are still there alongside the user note.
    assert any("Public API" in n or "Exception family" in n for n in report.entry.notes)


def test_yaml_uses_block_scalar_for_long_descriptions(tmp_path: Path) -> None:
    repo = _make_torpedo_min(tmp_path / "torp")
    long_desc = (
        "Torpedo (mini) — async chassis. Wraps Sanic. Builds an opinionated "
        "request shape and structured logging pipeline so consumers don't have "
        "to wire it up themselves."
    )
    result = runner.invoke(
        app,
        [
            "ai-context",
            "build",
            "--path",
            str(repo),
            "--description",
            long_desc,
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    raw = (Path.home() / ".changebrief" / "context.yaml").read_text(encoding="utf-8")
    # Block scalar (`>`) appears for long values rather than a single quoted line.
    assert ">" in raw
    # Round-trips cleanly back to the original description.
    saved = yaml.safe_load(raw)
    # YAML folded block scalars normalise newlines to spaces; check token presence.
    desc = saved["frameworks"]["torpedo"]
    assert "async chassis" in desc and "Sanic" in desc
