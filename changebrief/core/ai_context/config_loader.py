"""Optional org/repo overrides for the generated AI context.

Two layers of overrides, both optional, both YAML; the loader merges them
so a per-repo file can extend (rather than fully shadow) the per-user file.

Lookup:

1. **Explicit** ``--config <path>`` from the CLI — takes full precedence; no
   layering when this is set (the user asked for *that* file specifically).
2. **Per-repo** ``<repo_root>/.changebrief/context.yaml``
3. **Per-user** ``~/.changebrief/context.yaml``

When the explicit path is *not* set, layers 2 and 3 are merged with the
per-repo file winning per key:

* scalar fields (``project_summary``) — repo wins if set, else home.
* dict fields  (``frameworks``)       — repo entries override home for the
  same key; non-conflicting keys from both layers are kept.
* list fields  (``do``, ``dont``, ``notes``) — concatenated with home first,
  then repo, with simple de-duplication so re-running ``ai-context build``
  doesn't pile up duplicates.

The schema is intentionally tiny:

.. code-block:: yaml

    project_summary: "One-liner that overrides the auto-detected description."
    frameworks:
      torpedo: "Torpedo (Sanic-based async framework)."
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
from typing import Any, Dict, List, Optional

import yaml

from changebrief.core.ai_context.models import ContextConfig


def load_context_config(
    repo_root: str | Path,
    *,
    explicit_path: Optional[str | Path] = None,
) -> ContextConfig:
    """Return a :class:`ContextConfig`, layering home + repo when both exist."""
    if explicit_path is not None:
        explicit = Path(explicit_path).expanduser()
        if explicit.is_file():
            return _parse(_load(explicit))
        return ContextConfig()

    layers: List[Dict[str, Any]] = []
    home_path = Path.home() / ".changebrief" / "context.yaml"
    if home_path.is_file():
        layers.append(_load(home_path))

    root = Path(repo_root)
    for name in ("context.yaml", "context.yml"):
        repo_path = root / ".changebrief" / name
        if repo_path.is_file():
            layers.append(_load(repo_path))
            break

    if not layers:
        return ContextConfig()

    merged: Dict[str, Any] = {}
    for layer in layers:
        merged = _merge(merged, layer)
    return _parse(merged)


def _load(path: Path) -> Dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Layer ``override`` on top of ``base`` with field-aware semantics."""
    out: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if key in {"do", "dont", "notes"}:
            existing = out.get(key) or []
            if not isinstance(existing, list):
                existing = []
            new = value or []
            if not isinstance(new, list):
                continue
            seen: set[str] = set()
            combined: List[str] = []
            for item in list(existing) + list(new):
                key_str = str(item).strip()
                if not key_str or key_str in seen:
                    continue
                seen.add(key_str)
                combined.append(key_str)
            out[key] = combined
        elif key == "frameworks":
            existing = out.get(key) or {}
            if not isinstance(existing, dict):
                existing = {}
            new = value or {}
            if not isinstance(new, dict):
                continue
            merged_dict = dict(existing)
            merged_dict.update(new)
            out[key] = merged_dict
        else:
            if value is not None and value != "":
                out[key] = value
    return out


def _parse(raw: Dict[str, Any]) -> ContextConfig:
    if not isinstance(raw, dict):
        return ContextConfig()

    allowed = {f.name for f in fields(ContextConfig)}
    kwargs: Dict[str, Any] = {}
    for key in allowed:
        if key not in raw:
            continue
        value = raw[key]
        if key in {"do", "dont", "notes"}:
            kwargs[key] = [str(item).strip() for item in (value or []) if str(item).strip()]
        elif key == "project_summary":
            kwargs[key] = str(value).strip() if value else None
        elif key == "frameworks":
            if isinstance(value, dict):
                kwargs[key] = {
                    str(pkg).strip().lower(): str(desc).strip()
                    for pkg, desc in value.items()
                    if str(pkg).strip() and str(desc).strip()
                }
    return ContextConfig(**kwargs)
