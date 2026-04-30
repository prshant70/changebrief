"""Secret and PII redaction for content sent to external LLMs.

Best-effort, regex-based. The goal is to neutralise the most common high-risk
tokens (private keys, JWTs, vendor API keys, AWS access keys, ``key=value``
secret pairs, email addresses) before any content leaves the user's machine.

Designed to fail safe: if a pattern wrongly matches, the worst outcome is a
``[REDACTED:KIND]`` placeholder in the prompt — never a leaked secret.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Pattern, Tuple

REDACTED_TEMPLATE = "[REDACTED:{kind}]"


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: Pattern[str]
    kind: str
    keep_key: bool = False  # for kv-style rules: keep "key=" prefix


def _compile(rules: Iterable[Tuple[str, str, str, bool]]) -> list[_Rule]:
    return [
        _Rule(name=n, pattern=re.compile(p, re.MULTILINE), kind=k, keep_key=kk)
        for (n, p, k, kk) in rules
    ]


# Order matters: high-confidence patterns run first so they don't get masked
# by later, broader ones.
_RULES: list[_Rule] = _compile([
    (
        "private_key",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----",
        "PRIVATE_KEY",
        False,
    ),
    (
        "jwt",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        "JWT",
        False,
    ),
    ("openai_key", r"\bsk-[A-Za-z0-9]{20,}\b", "OPENAI_KEY", False),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b", "GITHUB_TOKEN", False),
    ("slack_token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "SLACK_TOKEN", False),
    (
        "stripe_key",
        r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b",
        "STRIPE_KEY",
        False,
    ),
    ("aws_access_key_id", r"\bAKIA[0-9A-Z]{16}\b", "AWS_ACCESS_KEY_ID", False),
    ("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}\b", "GOOGLE_API_KEY", False),
    (
        "bearer_token",
        r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{20,}\b",
        "BEARER_TOKEN",
        False,
    ),
    # key/value secrets — keep the key, redact the value
    (
        "kv_secret",
        r"(?i)\b(api[_-]?key|secret|passwd|password|access[_-]?token|auth[_-]?token|token)\b\s*[:=]\s*['\"]?([^\s'\"<>]{8,})['\"]?",
        "SECRET",
        True,
    ),
    (
        "email",
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
        "EMAIL",
        False,
    ),
])


def redact(text: str) -> str:
    """Return ``text`` with high-confidence secrets and PII replaced."""
    out, _ = redact_with_counts(text)
    return out


def redact_with_counts(text: str) -> tuple[str, dict[str, int]]:
    """Like :func:`redact`, but also return per-kind hit counts (for telemetry)."""
    if not text:
        return text or "", {}
    counts: dict[str, int] = {}
    out = text
    for rule in _RULES:
        hits = rule.pattern.findall(out)
        if not hits:
            continue
        counts[rule.kind] = counts.get(rule.kind, 0) + len(hits)
        placeholder = REDACTED_TEMPLATE.format(kind=rule.kind)
        if rule.keep_key:
            out = rule.pattern.sub(
                lambda m, ph=placeholder: f"{m.group(1)}={ph}",
                out,
            )
        else:
            out = rule.pattern.sub(placeholder, out)
    return out, counts
