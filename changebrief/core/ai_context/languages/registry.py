"""Ordered registry of language adapters.

Order matters only for deterministic output; detection itself is independent.
The :class:`GenericAdapter` MUST stay last because it always detects.
"""

from __future__ import annotations

from typing import List

from changebrief.core.ai_context.languages.base import LanguageAdapter
from changebrief.core.ai_context.languages.generic import GenericAdapter
from changebrief.core.ai_context.languages.go import GoAdapter
from changebrief.core.ai_context.languages.java import JavaAdapter
from changebrief.core.ai_context.languages.javascript import (
    JavaScriptAdapter,
    TypeScriptAdapter,
)
from changebrief.core.ai_context.languages.python import PythonAdapter
from changebrief.core.ai_context.languages.ruby import RubyAdapter
from changebrief.core.ai_context.languages.rust import RustAdapter


def get_adapters() -> List[LanguageAdapter]:
    return [
        PythonAdapter(),
        TypeScriptAdapter(),
        JavaScriptAdapter(),
        GoAdapter(),
        RustAdapter(),
        JavaAdapter(),
        RubyAdapter(),
        GenericAdapter(),  # MUST stay last
    ]
