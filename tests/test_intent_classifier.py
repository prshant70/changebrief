"""Tests for the intent classifier — multi-framework + false-positive resistance."""

from __future__ import annotations

from changebrief.core.analyzer.change_analyzer import ChangeSummary
from changebrief.core.analyzer.intent_classifier import classify_intent


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


# -- existing canonical cases (kept) -----------------------------------------


def test_intent_classifier_positive_signals() -> None:
    cs = ChangeSummary(
        files=["app.py"],
        functions=[],
        diff_text=_diff(
            "app.py",
            added=[
                '@app.post("/users")',
                "def create_user():",
                "    if not email:",
                '        raise BadRequest("missing email")',
                '    return {"ok": True}',
            ],
        ),
    )
    intent = classify_intent(cs)
    assert intent.intent_score >= 0.65
    assert intent.intent_label in {"intentional", "mixed"}
    assert intent.signals


def test_intent_classifier_negative_signals_refactor() -> None:
    cs = ChangeSummary(
        files=["x.py"],
        functions=[],
        diff_text=_diff(
            "x.py",
            removed=["if ok:", "    return 1"],
            added=["if ok and ready:", "    return 2"],
        ),
    )
    intent = classify_intent(cs)
    assert intent.intent_score <= 0.6


# -- multi-framework route detection -----------------------------------------


def test_intent_django_path_route() -> None:
    cs = ChangeSummary(
        files=["urls.py"],
        functions=[],
        diff_text=_diff(
            "urls.py",
            added=["    path('users/', views.users),"],
        ),
    )
    assert "new endpoint/route added" in classify_intent(cs).signals


def test_intent_express_route_typescript() -> None:
    cs = ChangeSummary(
        files=["server.ts"],
        functions=[],
        diff_text=_diff(
            "server.ts",
            added=["app.get('/health', (req, res) => res.json({ ok: true }))"],
        ),
    )
    assert "new endpoint/route added" in classify_intent(cs).signals


def test_intent_spring_get_mapping_java() -> None:
    cs = ChangeSummary(
        files=["UserController.java"],
        functions=[],
        diff_text=_diff(
            "UserController.java",
            added=['    @GetMapping("/users")'],
        ),
    )
    assert "new endpoint/route added" in classify_intent(cs).signals


def test_intent_go_handlefunc() -> None:
    cs = ChangeSummary(
        files=["server.go"],
        functions=[],
        diff_text=_diff(
            "server.go",
            added=['    http.HandleFunc("/users", handler)'],
        ),
    )
    assert "new endpoint/route added" in classify_intent(cs).signals


# -- false-positive resistance -----------------------------------------------


def test_intent_does_not_detect_route_in_markdown_comment() -> None:
    cs = ChangeSummary(
        files=["NOTES.md"],
        functions=[],
        diff_text=_diff(
            "NOTES.md",
            added=["- We previously used @app.post for /users"],
        ),
    )
    assert "new endpoint/route added" not in classify_intent(cs).signals


def test_intent_balanced_refactor_is_not_pure_deletion() -> None:
    cs = ChangeSummary(
        files=["a.py"],
        functions=[],
        diff_text=_diff(
            "a.py",
            removed=["def old():", "    return 1"],
            added=["def renamed():", "    return 1"],
        ),
    )
    intent = classify_intent(cs)
    assert "pure deletions (no additions)" not in intent.signals
    assert "deletions dominate the change" not in intent.signals


def test_intent_pure_deletion_lowers_score() -> None:
    cs = ChangeSummary(
        files=["a.py"],
        functions=[],
        diff_text=_diff(
            "a.py",
            removed=["def old_function():", "    return 1"],
        ),
    )
    intent = classify_intent(cs)
    assert intent.intent_score < 0.5
    assert any("pure deletions" in s for s in intent.signals)
