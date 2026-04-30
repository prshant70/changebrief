"""Tests for the confidence scorer."""

from __future__ import annotations

from changebrief.core.analyzer.change_analyzer import ChangeSummary
from changebrief.core.analyzer.confidence_scorer import compute_confidence
from changebrief.core.analyzer.intent_classifier import IntentSummary


def _diff(file: str, *, added=(), removed=()) -> str:
    lines = [
        f"diff --git a/{file} b/{file}",
        f"--- a/{file}",
        f"+++ b/{file}",
        "@@ -1,1 +1,1 @@",
    ]
    for line in removed:
        lines.append("-" + line)
    for line in added:
        lines.append("+" + line)
    return "\n".join(lines) + "\n"


def test_confidence_structural_and_localized_high() -> None:
    cs = ChangeSummary(
        files=["models.py"],
        functions=[],
        diff_text=_diff("models.py", added=["class User:", "    pass"]),
    )
    intent = IntentSummary(intent_score=0.7, intent_label="intentional", signals=["x"])
    conf = compute_confidence(cs, intent)
    assert conf.score >= 0.75
    assert conf.level == "High"
    assert conf.reasons


def test_confidence_large_change_lowish() -> None:
    cs = ChangeSummary(
        files=[f"f{i}.py" for i in range(12)],
        functions=[],
        diff_text="manager\n",
    )
    intent = IntentSummary(intent_score=0.3, intent_label="uncertain", signals=[])
    conf = compute_confidence(cs, intent)
    assert conf.level in {"Low", "Medium"}


def test_confidence_does_not_penalise_for_word_manager() -> None:
    """The previous heuristic docked confidence for any 'manager' in the diff."""
    cs = ChangeSummary(
        files=["service.py"],
        functions=[],
        diff_text=_diff(
            "service.py",
            added=[
                "class TransactionManager:",
                "    pass",
            ],
        ),
    )
    intent = IntentSummary(intent_score=0.6, intent_label="intentional", signals=[])
    conf = compute_confidence(cs, intent)
    assert "Indirect dependencies" not in " ".join(conf.reasons)
    assert conf.score >= 0.6  # localized + structural


def test_confidence_structural_does_not_match_css_class() -> None:
    """`class="..."` in CSS / HTML must NOT count as a structural code change."""
    cs = ChangeSummary(
        files=["site.css"],
        functions=[],
        diff_text=_diff(
            "site.css",
            added=[
                ".class { color: red; }",
                'a[class="button"] { display: block; }',
            ],
        ),
    )
    intent = IntentSummary(intent_score=0.5, intent_label="mixed", signals=[])
    conf = compute_confidence(cs, intent)
    structural_reason = "Clear structural change detected"
    assert structural_reason not in " ".join(conf.reasons)


def test_confidence_typescript_export_recognised_as_structural() -> None:
    cs = ChangeSummary(
        files=["api.ts"],
        functions=[],
        diff_text=_diff(
            "api.ts",
            added=["export function getUser(id: string) { return id; }"],
        ),
    )
    intent = IntentSummary(intent_score=0.7, intent_label="intentional", signals=[])
    conf = compute_confidence(cs, intent)
    assert "Clear structural change detected" in " ".join(conf.reasons)
