"""Centralized guards for enabling/disabling LLM usage."""

from __future__ import annotations

import os


def llm_disabled() -> bool:
    """
    Return True when LLM calls should be disabled.

    - `CHANGEBRIEF_DISABLE_LLM=1` disables all network LLM calls (useful for tests/CI).
    - When running under pytest (`PYTEST_CURRENT_TEST`), disable by default.
    """
    if os.getenv("CHANGEBRIEF_DISABLE_LLM", "").strip() in {"1", "true", "TRUE", "yes", "YES"}:
        return True
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    return False

