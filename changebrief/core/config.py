"""Load and save ChangeBrief YAML configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from changebrief.core.exceptions import ConfigError, ValidationError
from changebrief.utils.paths import get_config_file

ALLOWED_LLM_PROVIDERS = frozenset(
    {"anthropic", "azure", "google", "mistral", "ollama", "openai"},
)

_ALLOWED_LOG_LEVELS = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
)

DEFAULT_LLM_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_LOG_LEVEL = "INFO"

# Keys we persist; llm_api_key may be empty until set via `init`.
DEFAULTS: Dict[str, Any] = {
    "llm_provider": DEFAULT_LLM_PROVIDER,
    "llm_api_key": "",
    "default_model": DEFAULT_MODEL,
    "log_level": DEFAULT_LOG_LEVEL,
}


def validate_llm_provider_input(raw: str) -> str:
    """Normalize and validate a user-supplied provider name (e.g. from ``init``)."""
    p = (raw or "").strip().lower()
    if not p:
        raise ValidationError("LLM provider cannot be empty.")
    if p not in ALLOWED_LLM_PROVIDERS:
        allowed = ", ".join(sorted(ALLOWED_LLM_PROVIDERS))
        raise ValidationError(f"Provider must be one of: {allowed}.")
    return p


def config_path(explicit: Optional[Path] = None) -> Path:
    """Resolved config file path."""
    return explicit.expanduser().resolve() if explicit else get_config_file()


def load_config(explicit: Optional[Path] = None) -> Dict[str, Any]:
    """Load config from disk, merging with defaults for missing keys."""
    path = config_path(explicit)
    data = dict(DEFAULTS)
    if not path.is_file():
        return data
    try:
        raw = path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(raw) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not read config at {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"Config at {path} must be a mapping.")
    data.update({k: v for k, v in loaded.items() if k in DEFAULTS})
    return data


def save_config(
    data: Dict[str, Any],
    explicit: Optional[Path] = None,
) -> Path:
    """Write config to disk. Only known keys are persisted."""
    path = config_path(explicit)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {k: data.get(k, DEFAULTS[k]) for k in DEFAULTS}
    try:
        path.write_text(
            yaml.safe_dump(out, default_flow_style=False, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        raise ConfigError(f"Could not write config to {path}: {exc}") from exc
    return path


def mask_api_key(key: str) -> str:
    """Mask an API key for display (show last 4 chars when long enough)."""
    if not key:
        return "(not set)"
    if len(key) <= 4:
        return "****"
    return "****" + key[-4:]


def validate_config_values(data: Dict[str, Any]) -> list[str]:
    """
    Validate merged config values after load.

    Raises :class:`ConfigError` when required fields are invalid.
    Returns a list of non-fatal warnings (e.g. missing API key).
    """
    warnings: list[str] = []

    prov = str(data.get("llm_provider") or "").strip()
    if not prov:
        raise ConfigError("llm_provider must be a non-empty string.")

    level = str(data.get("log_level") or "INFO").upper()
    if level not in _ALLOWED_LOG_LEVELS:
        allowed = ", ".join(sorted(_ALLOWED_LOG_LEVELS))
        raise ConfigError(f"log_level must be one of: {allowed}; got {level!r}.")

    model = str(data.get("default_model") or "").strip()
    if not model:
        raise ConfigError("default_model must be a non-empty string.")

    if not str(data.get("llm_api_key") or "").strip():
        warnings.append(
            "llm_api_key is empty; set it with `changebrief init --force` or edit the config file.",
        )
    return warnings
