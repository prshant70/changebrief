"""Filesystem paths used by ChangeBrief."""

from pathlib import Path


def get_config_dir() -> Path:
    """Return the user config directory (~/.changebrief)."""
    return Path.home() / ".changebrief"


def get_config_file() -> Path:
    """Return the default config file path."""
    return get_config_dir() / "config.yaml"
