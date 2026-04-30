"""Tests for the ai-context generator (rendering + marker-based merge)."""

from __future__ import annotations

from changebrief.core.ai_context.generator import (
    AGENT_TARGETS,
    MARKER_END,
    MARKER_START,
    has_marker,
    merge_with_existing,
    render,
)
from changebrief.core.ai_context.models import AIContext, AIContextSection


def _sample_context() -> AIContext:
    return AIContext(
        project_name="demo",
        overview="A small demo for tests.",
        sections=[
            AIContextSection(
                title="Stack",
                bullets=["Python", "FastAPI"],
            ),
            AIContextSection(
                title="Validation mindset (ChangeBrief)",
                bullets=["Run changebrief validate before each PR."],
            ),
        ],
    )


def test_render_includes_per_agent_head_and_markers() -> None:
    target = AGENT_TARGETS["claude"]
    out = render(_sample_context(), target=target)
    assert "Read by Claude Code" in out
    assert MARKER_START in out and MARKER_END in out
    assert "## Stack" in out
    assert "## Validation mindset (ChangeBrief)" in out
    # Footer guides re-run safety.
    assert "Anything below this line is preserved" in out


def test_per_agent_head_matter_differs() -> None:
    body_a = render(_sample_context(), target=AGENT_TARGETS["claude"])
    body_b = render(_sample_context(), target=AGENT_TARGETS["cursor"])
    body_c = render(_sample_context(), target=AGENT_TARGETS["codex"])
    # Same body inside markers, different head matter outside.
    assert body_a != body_b != body_c
    assert "Cursor" in body_b
    assert "Codex" in body_c


def test_merge_preserves_user_content_outside_markers() -> None:
    target = AGENT_TARGETS["claude"]
    first = render(_sample_context(), target=target)
    # Simulate a user appending custom guidance after the footer.
    custom_tail = "\n## Team-specific\n- Always pair with backend on auth changes.\n"
    on_disk = first + custom_tail

    # Re-render with a different overview.
    new_ctx = _sample_context()
    new_ctx.overview = "An updated overview."
    second = render(new_ctx, target=target)

    merged = merge_with_existing(second, on_disk)
    assert "An updated overview." in merged
    assert "Always pair with backend on auth changes." in merged  # preserved


def test_merge_with_no_markers_returns_new_rendered() -> None:
    rendered = render(_sample_context(), target=AGENT_TARGETS["claude"])
    # File without markers — caller decides whether to overwrite (--force).
    merged = merge_with_existing(rendered, "hand-written file with no markers")
    assert merged == rendered


def test_has_marker_detects_block() -> None:
    rendered = render(_sample_context(), target=AGENT_TARGETS["claude"])
    assert has_marker(rendered) is True
    assert has_marker("plain text") is False
