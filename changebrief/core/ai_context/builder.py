"""Compose & persist a rich framework entry for ``~/.changebrief/context.yaml``.

Pipeline:

1. :func:`build_framework_entry` runs the deterministic AST extractor (always
   on) and the optional LLM synthesizer (when ``llm_api_key`` is set and
   ``CHANGEBRIEF_DISABLE_LLM`` is unset).
2. The extractor's hard facts (public API, exception family, Python pin,
   reference paths) become a baseline set of ``notes:`` so the no-LLM
   output is still useful.
3. The synthesizer layers on top: a real framework description, a few
   ``related_frameworks`` entries, and citation-verified ``do`` / ``dont``
   / ``notes`` bullets.
4. :func:`upsert_framework_entry` merges everything into the YAML, never
   silently overwriting the existing entry without ``--force`` and always
   preserving unrelated keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from changebrief.core.ai_context.build_extractor import (
    FrameworkExtraction,
    extract_framework,
)
from changebrief.core.ai_context.build_synthesizer import (
    SynthesisResult,
    synthesize,
)
from changebrief.core.ai_context.models import RepoContext


@dataclass
class FrameworkEntry:
    """The composed entry that gets written to ``~/.changebrief/context.yaml``.

    A ``description`` may legitimately be a multi-line block — we use a
    YAML block-scalar (``>-``) representer when serialising so consumers
    get the same readable shape as a hand-written file.
    """

    name: str
    description: str
    related_frameworks: Dict[str, str] = field(default_factory=dict)
    do: List[str] = field(default_factory=list)
    dont: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass
class BuildReport:
    """What ``build_framework_entry`` learned. Surfaced by the CLI for transparency."""

    extraction: FrameworkExtraction
    synthesis: SynthesisResult
    entry: FrameworkEntry


def build_framework_entry(
    repo_ctx: RepoContext,
    *,
    name_override: Optional[str] = None,
    description_override: Optional[str] = None,
    user_notes: Optional[List[str]] = None,
    config: Optional[dict] = None,
    llm_enabled: bool = True,
    model: Optional[str] = None,
) -> BuildReport:
    """Run the full extract + synthesize + compose pipeline.

    ``description_override`` and ``user_notes`` always win over LLM output;
    they let users curate the entry without fighting the model.
    """
    name = (name_override or _detect_package_name(repo_ctx) or "").strip()
    name = name.lower().replace("_", "-")
    if not name:
        raise ValueError(
            "Could not determine the framework's package name from the repo. "
            "Pass --name explicitly (e.g. --name torpedo)."
        )

    extraction = extract_framework(repo_ctx, package_name=name)

    synthesis = SynthesisResult()
    if llm_enabled and config is not None:
        synthesis = synthesize(extraction, config=config, model=model)

    description = (
        (description_override or "").strip()
        or (synthesis.framework_description or "").strip()
        or _fallback_description(extraction)
    )
    if not description:
        raise ValueError(
            "Could not derive a description from the repo. "
            "Pass --description explicitly."
        )

    notes = list(synthesis.notes)
    notes.extend(_baseline_notes(extraction, exclude=set(notes)))
    if user_notes:
        for note in user_notes:
            note = note.strip()
            if note and note not in notes:
                notes.append(note)

    entry = FrameworkEntry(
        name=name,
        description=description,
        related_frameworks=dict(synthesis.related_frameworks),
        do=list(synthesis.do),
        dont=list(synthesis.dont),
        notes=notes,
    )
    return BuildReport(extraction=extraction, synthesis=synthesis, entry=entry)


def upsert_framework_entry(
    entry: FrameworkEntry,
    config_path: Path,
    *,
    force: bool = False,
) -> tuple[bool, Path]:
    """Merge ``entry`` into ``config_path``, creating the file if missing.

    Returns ``(would_overwrite_existing, path_written_or_to_write)``.
    """
    existing = _load_yaml(config_path)
    frameworks = _coerce_dict(existing.get("frameworks"))

    if entry.name in frameworks and not force:
        return True, config_path

    merged = _merge_payload(existing, entry)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_dump_yaml(merged), encoding="utf-8")
    return False, config_path


def preview_merge(entry: FrameworkEntry, config_path: Path) -> str:
    """Return the YAML that would be written if we upserted ``entry``."""
    existing = _load_yaml(config_path)
    merged = _merge_payload(existing, entry)
    return _dump_yaml(merged).rstrip()


# ---------------------------------------------------------------------------- internals


def _merge_payload(existing: Dict[str, Any], entry: FrameworkEntry) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(existing)

    # frameworks: merge the framework's own description plus any related-frameworks entries.
    frameworks = _coerce_dict(merged.get("frameworks"))
    frameworks[entry.name] = entry.description
    for pkg, desc in entry.related_frameworks.items():
        if pkg == entry.name:
            continue
        if pkg in frameworks:
            continue
        frameworks[pkg] = desc
    merged["frameworks"] = frameworks

    # do / dont / notes: append, dedupe, preserve user-curated content.
    for key, additions in (("do", entry.do), ("dont", entry.dont), ("notes", entry.notes)):
        if not additions:
            continue
        existing_list = _coerce_list(merged.get(key))
        for item in additions:
            if item not in existing_list:
                existing_list.append(item)
        merged[key] = existing_list

    return merged


def _baseline_notes(
    extraction: FrameworkExtraction,
    *,
    exclude: set[str],
) -> List[str]:
    """Hard, citation-bearing facts that work even when the LLM is unavailable."""
    out: List[str] = []

    if extraction.public_api:
        names = ", ".join(f"`{s.name}`" for s in extraction.public_api[:18])
        path = (
            f"{extraction.package_dir}/__init__.py" if extraction.package_dir else "__init__.py"
        )
        out.append(f"**Public API** — {names}. Source: `{path}`.")

    if extraction.exceptions:
        names = ", ".join(f"`{e.name}`" for e in extraction.exceptions[:10])
        out.append(
            f"**Exception family** — subclass these for service errors: {names}."
        )

    if extraction.python_version_pin:
        out.append(
            f"**Python version** — `requires-python = \"{extraction.python_version_pin}\"`. "
            "Service repos using this framework must match."
        )

    if extraction.config_keys:
        keys = ", ".join(f"`{k}`" for k in extraction.config_keys[:10])
        out.append(f"**Config keys** — top-level `config.json` exposes {keys}.")

    if extraction.examples:
        ex_path = extraction.examples[0].rel_path
        # Keep paths repo-relative so cached dependency builds never leak local
        # filesystem paths into generated context.
        out.append(f"**Smallest end-to-end example**: `{ex_path}`.")

    if extraction.notable_dirs:
        bits = ", ".join(f"`{nd.rel_path}` ({nd.description})" for nd in extraction.notable_dirs[:5])
        out.append(f"**Reference paths** — {bits}.")

    return [n for n in out if n not in exclude]


def _fallback_description(extraction: FrameworkExtraction) -> str:
    """Best-effort description when neither the user nor the LLM provided one."""
    summary = (extraction.summary or "").strip()
    name = extraction.project_name or extraction.package_name
    if summary:
        text = summary
    elif extraction.primary_language:
        text = f"{name} — {extraction.primary_language.title()} package."
    else:
        text = name
    return text


def _detect_package_name(repo_ctx: RepoContext) -> Optional[str]:
    """Pick the import name (``kafka`` for distribution ``kafka-python``)."""
    profiles = [p for p in repo_ctx.profiles if p.language != "generic"]
    py = next((p for p in profiles if p.language == "python"), None)
    if py:
        proj = (repo_ctx.project_name or "").strip().lower().replace("-", "_")
        package_dirs: List[str] = []
        for src in py.source_dirs:
            top = src.split("/", 1)[0]
            if top in {"src", "lib", "app", "apps"}:
                continue
            package_dirs.append(top)
        for candidate in package_dirs:
            if candidate.replace("_", "").lower() == proj.replace("_", "").lower():
                return candidate
        if package_dirs:
            return package_dirs[0]
    if repo_ctx.project_name:
        return repo_ctx.project_name
    return None


# ---------------------------------------------------------------------------- yaml IO


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _coerce_dict(value: Any) -> Dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {}


def _coerce_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


class _BlockScalarStr(str):
    """A string that ``yaml.safe_dump`` will emit using a folded block scalar.

    Long descriptions become readable in the YAML file instead of one
    massively long quoted line.
    """


def _block_scalar_representer(dumper: yaml.SafeDumper, data: _BlockScalarStr) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style=">")


yaml.SafeDumper.add_representer(_BlockScalarStr, _block_scalar_representer)


def _dump_yaml(payload: Dict[str, Any]) -> str:
    """Dump ``payload`` to YAML, using a folded block scalar for long descriptions.

    Long ``frameworks:`` values are wrapped to keep the file readable;
    short values stay as inline strings.
    """
    transformed = _wrap_long_strings(payload)
    return yaml.safe_dump(
        transformed,
        default_flow_style=False,
        sort_keys=True,
        width=88,
        allow_unicode=True,
    )


def _wrap_long_strings(value: Any) -> Any:
    """Recursively replace long top-level strings with :class:`_BlockScalarStr`."""
    if isinstance(value, dict):
        return {k: _wrap_long_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_wrap_long_strings(item) for item in value]
    if isinstance(value, str) and (len(value) > 90 or "\n" in value):
        return _BlockScalarStr(value)
    return value
