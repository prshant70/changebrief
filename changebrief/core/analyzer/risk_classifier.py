"""Risk classification using diff-aware, language-aware heuristics.

Replaces the previous "lowercase the whole diff and substring-match" approach
that produced wide false positives (``"db"`` matching ``stub``/``doubt``,
``"http"`` matching every URL in a comment, ``"sql"`` matching ``mysql``).

Design:

- Patterns are anchored on word boundaries.
- They run against **added or removed source lines only** (not the full diff
  blob, which includes context lines and metadata).
- Each pattern is keyed by language and only applied to files in that
  language. A Python pattern never runs against ``.ts`` lines.
- File-bucket signals (config / migration / test deletion) are independent of
  per-line scanning so they cover cases the line patterns can miss.

Severity is calibrated by both file count and lines changed, with category
multipliers for high-impact buckets (persistence, auth/secrets, test loss).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Pattern

from changebrief.core.analyzer.change_analyzer import ChangeSummary
from changebrief.core.analyzer.diff_utils import (
    DiffLine,
    is_config_file,
    is_migration_file,
    is_test_file,
    iter_added_lines,
    iter_removed_lines,
    language_of,
)


@dataclass
class RiskSummary:
    level: str  # low | medium | high
    types: List[str]


# ---------------------------------------------------------------------------
# Per-language, word-anchored patterns.
# ---------------------------------------------------------------------------

_PERSISTENCE: Dict[str, Pattern[str]] = {
    "python": re.compile(
        r"\b(?:"
        r"session\.(?:add|commit|delete|merge|query|flush|rollback)"
        r"|engine\.execute|connection\.execute|cursor\.execute"
        r"|db\.session"
        r"|transaction\.(?:atomic|commit|rollback)"
        r"|\w+\.objects\.(?:create|filter|get|update|delete|all)"
        r"|\w+\.save\s*\("
        r")\b"
    ),
    "javascript": re.compile(
        r"\b(?:knex|prisma|sequelize|typeorm|mongoose)\b"
        r"|\.(?:execute|query|insert|update|delete)\s*\("
    ),
    "typescript": re.compile(
        r"\b(?:knex|prisma|sequelize|typeorm|mongoose)\b"
        r"|\.(?:execute|query|insert|update|delete)\s*\("
    ),
    "go": re.compile(
        r"\b(?:db\.(?:Exec|Query|QueryRow|Begin)"
        r"|tx\.(?:Commit|Rollback|Exec|Query)"
        r"|gorm\.\w+|sqlx\.\w+)\b"
    ),
    "java": re.compile(
        r"\b(?:jdbcTemplate|EntityManager|Hibernate|Repository)\b"
        r"|@Transactional\b"
    ),
    "ruby": re.compile(r"\b(?:ActiveRecord|\.save!?\b|\.update!?\b|\.destroy!?\b)"),
    "sql": re.compile(
        r"\b(?:CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE|CREATE\s+INDEX|"
        r"INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b",
        re.IGNORECASE,
    ),
}

_ERROR_HANDLING: Dict[str, Pattern[str]] = {
    "python": re.compile(r"^\s*(?:try\s*:|except\b|raise\b|finally\s*:)"),
    "javascript": re.compile(r"\b(?:try\s*\{|catch\s*\(|throw\s+(?:new\s+)?\w+|finally\s*\{)"),
    "typescript": re.compile(r"\b(?:try\s*\{|catch\s*\(|throw\s+(?:new\s+)?\w+|finally\s*\{)"),
    "go": re.compile(
        r"\b(?:if\s+err\s*!=\s*nil|errors\.(?:Is|As|New|Wrap)|panic\s*\(|recover\s*\(\))"
    ),
    "java": re.compile(r"\b(?:try\s*\{|catch\s*\(|throw\s+(?:new\s+)?\w+|throws\s+\w+)"),
    "ruby": re.compile(r"\b(?:begin\b|rescue\b|raise\b|ensure\b)"),
}

_EXTERNAL_CALLS: Dict[str, Pattern[str]] = {
    "python": re.compile(
        r"\b(?:requests|httpx|aiohttp|urllib\.(?:request|parse)|grpc|boto3|"
        r"redis|kafka|pika|celery)\.\w+"
    ),
    "javascript": re.compile(
        r"\b(?:fetch\s*\(|axios\.\w+|got\.\w+|node-fetch|grpc\.\w+|kafka\.\w+|redis\.\w+)"
    ),
    "typescript": re.compile(
        r"\b(?:fetch\s*\(|axios\.\w+|got\.\w+|node-fetch|grpc\.\w+|kafka\.\w+|redis\.\w+)"
    ),
    "go": re.compile(
        r"\b(?:http\.(?:Get|Post|Do|NewRequest|Client)|grpc\.Dial|client\.Do)\b"
    ),
    "java": re.compile(r"\b(?:HttpClient|RestTemplate|WebClient)\b"),
    "ruby": re.compile(r"\b(?:Net::HTTP|HTTParty|Faraday)\b"),
}

# Auth / secret-handling code is risky regardless of language; one regex.
_AUTH_RE = re.compile(
    r"\b(?:authenticate|authorization|jwt|bcrypt|hashlib|hmac|signin|signup|"
    r"login|password|secret|cipher|encrypt|decrypt|oauth|sso|"
    r"sign_in|sign_up)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _scan_per_language(lines: List[DiffLine], pattern_map: Dict[str, Pattern[str]]) -> bool:
    for ln in lines:
        pat = pattern_map.get(language_of(ln.file))
        if pat is not None and pat.search(ln.text):
            return True
    return False


def _scan_any(lines: List[DiffLine], pattern: Pattern[str]) -> bool:
    return any(pattern.search(ln.text) for ln in lines)


def classify_risk(change_summary: ChangeSummary) -> RiskSummary:
    """Classify regression risk for a change.

    Returns a :class:`RiskSummary` with a ``level`` of ``low | medium | high``
    and a list of human-readable category labels evidenced by the diff.
    """
    diff = getattr(change_summary, "diff_text", "") or ""
    files = list(getattr(change_summary, "files", []) or [])

    added = list(iter_added_lines(diff))
    removed = list(iter_removed_lines(diff))
    all_changes: List[DiffLine] = added + removed

    types: List[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        if t not in seen:
            seen.add(t)
            types.append(t)

    if _scan_per_language(all_changes, _ERROR_HANDLING):
        add("error handling change")
    if _scan_per_language(all_changes, _PERSISTENCE) or any(is_migration_file(f) for f in files):
        add("data persistence change")
    if _scan_per_language(all_changes, _EXTERNAL_CALLS):
        add("external call change")
    if _scan_any(all_changes, _AUTH_RE):
        add("auth/secrets change")
    if any(is_config_file(f) for f in files):
        add("configuration change")

    # Test-coverage reduction: more test lines removed than added across test files.
    test_removed = sum(1 for ln in removed if is_test_file(ln.file))
    test_added = sum(1 for ln in added if is_test_file(ln.file))
    if test_removed > test_added and test_removed >= 3:
        add("test coverage reduced")

    # ------------------------------------------------------------------ severity
    n_files = len(files)
    n_changes = len(added) + len(removed)

    has_high_impact = any(
        t in seen
        for t in (
            "data persistence change",
            "auth/secrets change",
            "test coverage reduced",
        )
    )

    level = "low"
    if n_files >= 8 or n_changes >= 200:
        level = "high"
    elif n_files >= 3 or n_changes >= 60:
        level = "medium"

    if has_high_impact:
        level = "high" if (n_files >= 3 or n_changes >= 60) else "medium"

    return RiskSummary(level=level, types=types or ["unknown"])
