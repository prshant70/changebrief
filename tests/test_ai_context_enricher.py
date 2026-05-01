"""Tests for ai-context LLM enrichment.

We don't actually call the LLM — we monkey-patch ``_call_llm`` to return
controlled payloads. These tests verify:

* Hallucinated paths get dropped (the safety net).
* Real paths flow through into new sections.
* Enrichment fails open (returns the unmodified context) on bad input.
* The ``--enrich`` flag is wired into the CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from changebrief.core.ai_context import enricher as enricher_mod
from changebrief.core.ai_context.composer import compose_context
from changebrief.core.ai_context.config_loader import load_context_config
from changebrief.core.ai_context.enricher import enrich
from changebrief.core.ai_context.scanner import scan_repo


@pytest.fixture()
def small_repo(tmp_path: Path) -> Path:
    """A self-contained tiny Python repo we can enrich."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndescription = "demo"\ndependencies = ["fastapi", "pytest"]\n',
        encoding="utf-8",
    )
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/x')\ndef x(): return 1\n",
        encoding="utf-8",
    )
    (pkg / "errors.py").write_text(
        "class DemoError(Exception):\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("# demo\nA tiny demo service.\n", encoding="utf-8")
    return tmp_path


def _force_llm_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend we have credentials and bypass the pytest-detection guard."""
    monkeypatch.setattr(enricher_mod, "llm_disabled", lambda: False)


def _config() -> dict:
    return {"llm_api_key": "sk-test", "default_model": "gpt-4o-mini"}


def test_enrich_drops_hallucinated_paths(monkeypatch: pytest.MonkeyPatch, small_repo: Path) -> None:
    _force_llm_enabled(monkeypatch)
    payload: Dict[str, Any] = {
        "polished_overview": "demo is a tiny FastAPI service exposing /x.",
        "inferred_conventions": [
            {"observation": "Routes live in `demo/main.py`", "evidence_path": "demo/main.py"},
            {
                "observation": "All errors subclass DemoError",
                "evidence_path": "src/imaginary/errors.py",  # NOT in repo — must be dropped
            },
        ],
        "gotchas": [
            {"description": "Don't add new top-level errors", "evidence_path": "demo/errors.py"},
            {"description": "Made-up rule", "evidence_path": "../etc/passwd"},  # path-traversal
        ],
        "do": [
            {"bullet": "Use the canonical entrypoint `demo/main.py`.", "evidence_path": "demo/main.py"},
            {"bullet": "Fake rule", "evidence_path": "nope.py"},
        ],
        "dont": [
            {"bullet": "Don't swallow `DemoError`.", "evidence_path": "demo/errors.py"},
            {"bullet": "Fake rule", "evidence_path": "nope.py"},
        ],
        "notes": [
            {"bullet": "The service is a FastAPI app defined in `demo/main.py`.", "evidence_path": "demo/main.py"},
            {"bullet": "Fake note", "evidence_path": "nope.py"},
        ],
    }
    monkeypatch.setattr(enricher_mod, "_call_llm", lambda *a, **kw: payload)

    ctx = scan_repo(small_repo)
    ai_ctx = compose_context(ctx, load_context_config(small_repo))
    enriched, result = enrich(ai_ctx, ctx, config=_config())

    assert result.used_llm is True
    assert result.convs_kept == 1
    assert result.convs_dropped == 1
    assert result.gotchas_kept == 1
    assert result.gotchas_dropped == 1

    # Polished overview applied.
    assert "FastAPI" in enriched.overview
    # New sections added (cited).
    titles = [s.title for s in enriched.sections]
    assert "Suggestions (LLM; verify in cited file)" in titles
    assert "Gotchas (LLM; verify in cited file)" in titles

    flat = "\n".join(b for s in enriched.sections for b in s.bullets)
    # Real-path bullets survived.
    assert "demo/main.py" in flat
    assert "demo/errors.py" in flat
    # Hallucinated path is gone.
    assert "imaginary" not in flat
    assert "etc/passwd" not in flat


def test_enrich_fails_open_on_non_dict(monkeypatch: pytest.MonkeyPatch, small_repo: Path) -> None:
    _force_llm_enabled(monkeypatch)
    monkeypatch.setattr(enricher_mod, "_call_llm", lambda *a, **kw: None)

    ctx = scan_repo(small_repo)
    ai_ctx = compose_context(ctx, load_context_config(small_repo))
    enriched, result = enrich(ai_ctx, ctx, config=_config())

    assert result.used_llm is False
    assert "unparseable" in (result.reason_skipped or "").lower()
    # Same context returned (no new sections added).
    assert enriched is ai_ctx


def test_enrich_skipped_when_no_api_key(monkeypatch: pytest.MonkeyPatch, small_repo: Path) -> None:
    _force_llm_enabled(monkeypatch)
    ctx = scan_repo(small_repo)
    ai_ctx = compose_context(ctx, load_context_config(small_repo))
    enriched, result = enrich(ai_ctx, ctx, config={"llm_api_key": ""})
    assert result.used_llm is False
    assert "llm_api_key" in (result.reason_skipped or "")
    assert enriched is ai_ctx


def test_enrich_skipped_when_llm_disabled(small_repo: Path) -> None:
    # Default test environment has llm_disabled() -> True (PYTEST_CURRENT_TEST set).
    ctx = scan_repo(small_repo)
    ai_ctx = compose_context(ctx, load_context_config(small_repo))
    enriched, result = enrich(ai_ctx, ctx, config=_config())
    assert result.used_llm is False
    assert "disabled" in (result.reason_skipped or "").lower()
    assert enriched is ai_ctx


def test_cache_hit_avoids_second_llm_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, small_repo: Path) -> None:
    _force_llm_enabled(monkeypatch)
    # Redirect the cache root to a clean tmp dir so this test doesn't pollute ~/.changebrief.
    monkeypatch.setattr(enricher_mod, "get_config_dir", lambda: tmp_path / ".changebrief")

    calls = {"n": 0}

    def fake_call(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        calls["n"] += 1
        return {
            "polished_overview": "demo (cached test)",
            "inferred_conventions": [{"observation": "x", "evidence_path": "demo/main.py"}],
            "gotchas": [],
            "do": [],
            "dont": [],
            "notes": [],
        }

    monkeypatch.setattr(enricher_mod, "_call_llm", fake_call)

    ctx = scan_repo(small_repo)
    ai_ctx = compose_context(ctx, load_context_config(small_repo))

    _, r1 = enrich(ai_ctx, ctx, config=_config())
    _, r2 = enrich(ai_ctx, ctx, config=_config())

    assert calls["n"] == 1, "second call should hit cache"
    assert r1.cache_hit is False
    assert r2.cache_hit is True


def test_cli_enrich_flag_runs_through_safely(monkeypatch: pytest.MonkeyPatch, small_repo: Path) -> None:
    """`--enrich` must not break the command when the LLM is disabled."""
    from typer.testing import CliRunner

    from changebrief.cli import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["ai-context", "init", "--path", str(small_repo), "--enrich", "--targets", "claude"],
    )
    assert result.exit_code == 0, result.output
    assert "enrichment skipped" in result.output  # LLM disabled under pytest
    assert (small_repo / "CLAUDE.md").exists()
