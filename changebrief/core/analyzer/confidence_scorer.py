"""Deterministic confidence scoring for validation analysis.

This score reflects *how much we trust the analysis*, not how risky the
change is. It's used to soften merge-risk claims when the heuristics have
little evidence to go on.

Improvements over the previous version:

- Structural detection runs against **added source lines**, language-aware,
  with declaration-style anchors (``def``, ``class``, ``func``, route
  decorators, ``CREATE TABLE``…). Previously a substring match on ``"class "``
  could trigger on an HTML/CSS line or even a comment.
- Removed the ``"manager" → -0.1`` rule. It was triggered by any variable
  named ``transaction_manager``, ``context_manager``, etc., and was not a
  meaningful signal.
- Added a small bonus for changes that touch tests as well — those usually
  come with author intent we can corroborate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Pattern

from changebrief.core.analyzer.diff_utils import (
    DiffLine,
    is_test_file,
    iter_added_lines,
    language_of,
)


@dataclass
class ConfidenceSummary:
    score: float  # 0.0 - 1.0
    level: str  # "High" | "Medium" | "Low"
    reasons: List[str]


_STRUCTURAL: Dict[str, Pattern[str]] = {
    "python": re.compile(
        r"^\s*(?:"
        r"class\s+\w+|"
        r"def\s+\w+|"
        r"async\s+def\s+\w+|"
        r"@\w[\w.]*\s*\(?|"
        r"(?:app|router|api|blueprint|bp)\.(?:get|post|put|delete|patch|route)\s*\("
        r")"
    ),
    "javascript": re.compile(
        r"^\s*(?:"
        r"class\s+\w+|"
        r"function\s+\w+|"
        r"export\s+(?:default\s+)?(?:class|function|const|let|var|async\s+function)\b|"
        r"(?:app|router)\.(?:get|post|put|delete|patch|use)\s*\("
        r")"
    ),
    "typescript": re.compile(
        r"^\s*(?:"
        r"class\s+\w+|"
        r"interface\s+\w+|"
        r"type\s+\w+\s*=|"
        r"function\s+\w+|"
        r"export\s+(?:default\s+)?(?:class|function|const|interface|type|enum)\b|"
        r"@\w+\s*\("
        r")"
    ),
    "go": re.compile(
        r"^\s*(?:"
        r"func\s+(?:\([^)]+\)\s+)?\w+|"
        r"type\s+\w+\s+(?:struct|interface)\b|"
        r"http\.HandleFunc\s*\("
        r")"
    ),
    "java": re.compile(
        r"^\s*(?:"
        r"(?:public|private|protected)\s+(?:static\s+)?(?:final\s+)?"
        r"(?:class|interface|enum|record)\s+\w+|"
        r"@(?:Get|Post|Put|Delete|Patch|Request)Mapping\b"
        r")"
    ),
    "rust": re.compile(r"^\s*(?:pub\s+)?(?:fn\s+\w+|struct\s+\w+|enum\s+\w+|trait\s+\w+)"),
    "ruby": re.compile(r"^\s*(?:class\s+\w+|module\s+\w+|def\s+\w+)"),
    "sql": re.compile(
        r"^\s*(?:CREATE|ALTER|DROP)\s+(?:TABLE|INDEX|VIEW|FUNCTION|PROCEDURE)\b",
        re.IGNORECASE,
    ),
}


def _has_structural_change(added: List[DiffLine]) -> bool:
    for ln in added:
        pat = _STRUCTURAL.get(language_of(ln.file))
        if pat is not None and pat.search(ln.text):
            return True
    return False


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _level(score: float) -> str:
    if score >= 0.75:
        return "High"
    if score >= 0.5:
        return "Medium"
    return "Low"


def compute_confidence(change_summary, intent_summary) -> ConfidenceSummary:
    """Compute an explainable confidence score for the analysis."""
    score = 0.5
    reasons: List[str] = []

    files = list(getattr(change_summary, "files", []) or [])
    added = list(iter_added_lines(getattr(change_summary, "diff_text", "")))

    if _has_structural_change(added):
        score += 0.2
        reasons.append("Clear structural change detected (declarations / route handlers)")

    intent_score = float(getattr(intent_summary, "intent_score", 0.5))
    if intent_score >= 0.65:
        score += 0.2
        reasons.append("Strong intent signals present")
    elif intent_score < 0.4:
        score -= 0.2
        reasons.append("Weak or unclear intent signals")

    if 1 <= len(files) <= 3:
        score += 0.15
        reasons.append("Change is localized")
    elif len(files) >= 9:
        score -= 0.25
        reasons.append("Large multi-file change reduces certainty")
    elif len(files) >= 6:
        score -= 0.1
        reasons.append("Multi-file change reduces certainty")

    if any(is_test_file(f) for f in files) and added:
        score += 0.05
        reasons.append("Test changes provide additional evidence")

    if not added and not files:
        score = 0.4
        reasons = ["No source-line changes detected"]

    score = _clamp(score)
    level = _level(score)
    if not reasons:
        reasons.append("Limited explicit signals; baseline heuristic used")
    return ConfidenceSummary(score=score, level=level, reasons=reasons)
