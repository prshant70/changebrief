"""Lightweight intent scoring: intentional change vs unintended regression.

Improvements over the previous version:

- Multi-framework route detection (Flask/FastAPI ``@app.route``, Django
  ``path(`` / ``url(``, Express ``app.get(``, Spring ``@GetMapping``,
  Go ``http.HandleFunc``, etc.) instead of just ``@app.`` / ``router.``.
- Patterns run only against **added or removed source lines** (not the full
  diff blob), so identifiers/comments don't trigger false signals.
- Capped, asymmetric deletion penalty: pure deletions read as risky, but
  ordinary refactors that delete a few lines while adding more are not
  punished into "uncertain".
- Removed the tautological "if/return appears in both added and removed text"
  signal, which fired on virtually every non-trivial diff.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Pattern

from changebrief.core.analyzer.diff_utils import (
    DiffLine,
    is_test_file,
    iter_added_lines,
    iter_removed_lines,
    language_of,
)


@dataclass
class IntentSummary:
    intent_score: float  # 0.0 - 1.0
    intent_label: str  # "intentional" | "mixed" | "uncertain"
    signals: List[str]


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _label(score: float) -> str:
    if score >= 0.65:
        return "intentional"
    if score >= 0.4:
        return "mixed"
    return "uncertain"


# ---------------------------------------------------------------------------
# Multi-framework route patterns. Keyed by language; each value is a list of
# alternative regexes — any one match indicates a new endpoint signal.
# ---------------------------------------------------------------------------

_ROUTE_PATTERNS: dict[str, list[Pattern[str]]] = {
    "python": [
        # Flask / FastAPI / many "router.method" frameworks
        re.compile(
            r"@(?:app|router|api|blueprint|bp)\.(?:route|get|post|put|delete|patch|websocket)\s*\("
        ),
        # Flask add_url_rule
        re.compile(r"\bapp\.add_url_rule\s*\("),
        # Django path()/url()/re_path()
        re.compile(r"\b(?:path|re_path|url)\s*\(\s*[r]?[\"']"),
        # FastAPI APIRouter declaration
        re.compile(r"\bAPIRouter\s*\("),
        # gRPC service/RPC declarations
        re.compile(r"\bclass\s+\w+Servicer\b|@grpc\.\w+"),
    ],
    "javascript": [
        re.compile(r"\b(?:app|router|api)\.(?:get|post|put|delete|patch|all|use|head|options)\s*\("),
        re.compile(r"\bcreateRouter\s*\("),
        re.compile(r"\bexpress\(\)\s*\.|express\.Router\(\)"),
    ],
    "typescript": [
        re.compile(r"\b(?:app|router|api)\.(?:get|post|put|delete|patch|all|use|head|options)\s*\("),
        re.compile(
            r"@(?:Get|Post|Put|Delete|Patch|All|Head|Options|Controller)\s*\("
        ),  # NestJS
    ],
    "go": [
        re.compile(r"\bhttp\.HandleFunc\s*\("),
        re.compile(r"\b(?:r|router|mux|engine|e)\.(?:GET|POST|PUT|DELETE|PATCH|Handle|HandleFunc)\s*\("),
    ],
    "java": [
        re.compile(r"@(?:GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)\b"),
        re.compile(r"@(?:RestController|Controller|Path)\b"),
    ],
    "kotlin": [
        re.compile(r"@(?:GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)\b"),
    ],
    "ruby": [
        re.compile(r"^\s*(?:get|post|put|delete|patch)\s+[\"'][/:][^\"']+[\"']"),
        re.compile(r"\bresources?\s+:[a-z_]+"),
    ],
}


def _has_route(lines: Iterable[DiffLine]) -> bool:
    for ln in lines:
        for pat in _ROUTE_PATTERNS.get(language_of(ln.file), ()):  # type: ignore[arg-type]
            if pat.search(ln.text):
                return True
    return False


# Validation/guard patterns — added input-checking is a strong intentional signal.
_VALIDATION_RE = re.compile(
    r"\braise\s+(?:[A-Z]\w*(?:Error|Exception)|BadRequest|ValidationError)\b"
    r"|\bthrow\s+new\s+\w+\s*\("
    r"|^\s*if\s+not\s+\w[\w.]*\s*[:\)]?"
    r"|^\s*if\s+\w[\w.]*\s+is\s+None\b"
    r"|\.is_valid\s*\(\s*\)"
    r"|\bvalidate(?:_\w+)?\s*\("
    r"|\brequired\s*=\s*True\b",
    re.MULTILINE,
)


def _git_last_commit_message(repo: Path, ref: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--pretty=%B", ref],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except Exception:
        pass
    return ""


def classify_intent(
    change_summary,
    *,
    repo_path: Optional[str] = None,
    feature_ref: Optional[str] = None,
) -> IntentSummary:
    """Deterministic, rule-based intent scoring."""
    diff_text = getattr(change_summary, "diff_text", "") or ""
    files = list(getattr(change_summary, "files", []) or [])

    added = list(iter_added_lines(diff_text))
    removed = list(iter_removed_lines(diff_text))
    n_added = len(added)
    n_removed = len(removed)

    score = 0.5
    signals: list[str] = []

    if _has_route(added):
        score += 0.3
        signals.append("new endpoint/route added")

    if any(_VALIDATION_RE.search(ln.text) for ln in added):
        score += 0.2
        signals.append("validation strengthened")

    if any(is_test_file(f) for f in files) and n_added > 0:
        score += 0.1
        signals.append("test changes accompany code")

    if any(f.lower().endswith(".sql") or "migration" in f.lower() for f in files):
        score += 0.15
        signals.append("schema/migration touched")

    # Asymmetric deletion penalty: only flag if the change is *predominantly*
    # deletion. Refactors with ~equal add/remove are NOT punished.
    if n_added == 0 and n_removed > 0:
        score -= 0.25
        signals.append("pure deletions (no additions)")
    elif n_removed >= 20 and n_removed > n_added * 2:
        score -= 0.15
        signals.append("deletions dominate the change")

    # Optional commit-message hint (kept light, both directions).
    repo = Path(repo_path).expanduser().resolve() if repo_path else None
    ref = feature_ref or "HEAD"
    msg = _git_last_commit_message(repo, ref) if repo else ""
    if msg:
        mlow = msg.lower()
        if re.search(r"\b(add|adds|added|introduce|implement|feat|feature)\b", mlow):
            score += 0.1
            signals.append("commit message indicates intentional addition")
        if re.search(r"\b(revert|rollback|rolled back)\b", mlow):
            score -= 0.15
            signals.append("commit message indicates revert")

    score = _clamp(score)
    return IntentSummary(intent_score=score, intent_label=_label(score), signals=signals)
