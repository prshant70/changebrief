"""CLI-facing request payloads passed into services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ValidateRequest:
    base: str
    feature: str
    path: Optional[str] = None
    nocache: bool = False
