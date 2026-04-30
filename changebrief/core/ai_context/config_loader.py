"""Optional org/repo overrides for the generated AI context.

Supported lookup order (first hit wins):

1. Explicit ``--config <path>`` from the CLI.
2. ``<repo_root>/.changebrief/context.yaml``
3. ``~/.changebrief/context.yaml`` (org-level personal default)

The schema is intentionally tiny:

.. code-block:: yaml

    project_summary: "One-liner that overrides the auto-detected description."
    do:
      - "Always wrap LLM calls with the redaction module."
    dont:
      - "Do not edit the lockfile by hand."
    notes:
      - "Run `changebrief validate ...` before opening a PR."

Anything else in the file is ignored (forward-compatible).
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Iterable, Optional

import yaml

from changebrief.core.ai_context.models import ContextConfig


def load_context_config(
    repo_root: str | Path,
    *,
    explicit_path: Optional[str | Path] = None,
) -> ContextConfig:
    """Return a :class:`ContextConfig`. Returns an empty one when no file exists."""
    candidates = _candidate_paths(repo_root, explicit_path)
    for path in candidates:
        if path.is_file():
            return _parse(path)
    return ContextConfig()


def _candidate_paths(repo_root: str | Path, explicit_path: Optional[str | Path]) -> Iterable[Path]:
    if explicit_path is not None:
        yield Path(explicit_path).expanduser()
        return
    root = Path(repo_root)
    yield root / ".changebrief" / "context.yaml"
    yield root / ".changebrief" / "context.yml"
    yield Path.home() / ".changebrief" / "context.yaml"


def _parse(path: Path) -> ContextConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ContextConfig()
    if not isinstance(raw, dict):
        return ContextConfig()

    allowed = {f.name for f in fields(ContextConfig)}
    kwargs = {}
    for key in allowed:
        if key not in raw:
            continue
        value = raw[key]
        if key in {"do", "dont", "notes"}:
            kwargs[key] = [str(item).strip() for item in (value or []) if str(item).strip()]
        elif key == "project_summary":
            kwargs[key] = str(value).strip() if value else None
        elif key == "frameworks":
            # Map of package_name -> friendly description.
            if isinstance(value, dict):
                kwargs[key] = {
                    str(pkg).strip().lower(): str(desc).strip()
                    for pkg, desc in value.items()
                    if str(pkg).strip() and str(desc).strip()
                }
    return ContextConfig(**kwargs)
