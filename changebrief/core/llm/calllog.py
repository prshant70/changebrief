"""LLM call logging (local, append-only).

This module records usage metadata for each LLM API call so operators can
understand cost and prompt budgets over time.

Design:
- Append-only JSONL under ``~/.changebrief/llm-calllog.jsonl``.
- Best-effort: failures never break the CLI.
- No prompt/response content is recorded here (counts + metadata only).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from changebrief.utils.paths import get_config_dir


@dataclass(frozen=True)
class LLMCallUsage:
    provider: str
    model: str
    purpose: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    temperature: Optional[float] = None
    tool_round: Optional[int] = None
    meta: Dict[str, Any] = field(default_factory=dict)
    timestamp_s: float = field(default_factory=lambda: time.time())


def log_llm_call_usage(usage: LLMCallUsage) -> None:
    """Append usage metadata to the local JSONL call log."""
    path = get_config_dir() / "llm-calllog.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(json.dumps(asdict(usage), ensure_ascii=False) + "\n")
    except OSError:
        return

