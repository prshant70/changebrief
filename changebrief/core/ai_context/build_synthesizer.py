"""LLM synthesis pass for ``ai-context build``.

Turns the deterministic :class:`FrameworkExtraction` into an idiomatic
``frameworks:`` description plus ``do:`` / ``dont:`` / ``notes:`` rules
suitable for ``~/.changebrief/context.yaml``.

Design rules (mirror the validation planner / ai-context enricher):

* JSON-schema enforced output (``response_format=json_schema``).
* Every bullet must list ``cites: [<repo-relative path>, ...]``. We verify
  each cited path resolves to a real file inside the framework repo and
  drop bullets whose citations are entirely hallucinated.
* All inbound prompt content is redacted by the OpenAI helper.
* Fail-open: any error returns an empty :class:`SynthesisResult` so the
  CLI just falls back to the deterministic baseline.
* Cached on ``(framework root, prompt version, model, content digest)``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from changebrief.core.ai_context.build_extractor import FrameworkExtraction
from changebrief.core.llm.guard import llm_disabled
from changebrief.utils.paths import get_config_dir


LOG = logging.getLogger("changebrief")

PROMPT_VERSION = "build-1"
CACHE_DIR_NAME = "ai-context-build"
CACHE_TTL_S = 7 * 24 * 60 * 60

# Caps to keep the YAML focused. The sample contexts that work well in the
# wild have ~3-8 do bullets, ~3-7 don't bullets, ~3-6 notes.
MAX_DO = 8
MAX_DONT = 7
MAX_NOTES = 6
MAX_RELATED_FRAMEWORKS = 5


SYSTEM_PROMPT = """\
You are reading the source of a code framework / library so an AI coding
agent can write IDIOMATIC code against it in OTHER repositories.

You will receive:
- Verified facts (public API surface, exception classes, notable
  subdirectories, config keys) extracted by a deterministic AST walker.
- The README (truncated).
- One or two short docs files (truncated).
- One or two example files (truncated).

Your output is a JSON document that will be merged into a home-level
`context.yaml`. It is read every time `changebrief ai-context init` runs
in a consumer repo that imports this framework, so the entries must be:

1. SPECIFIC: cite real symbols (`torpedo.Torpedo`, `BaseAPIClient`,
   `send_response`) and real files. No generic best-practices.
2. ACTIONABLE: each `do` / `dont` should help an agent make a concrete
   choice (which class to subclass, which decorator to use, which API
   to avoid).
3. GROUNDED: every bullet MUST include `cites: [<paths>]` listing one
   or more files where the claim is supported. Use only paths that
   appear in the input. No invented paths.
4. CONCISE: 1-2 sentences per bullet. Use backticks around code symbols.

Sections:
- `framework_description`: one paragraph (2-4 sentences) describing
   what the framework is, what it wraps, and how it bootstraps. Mention
   the entry-point symbol and the primary base classes.
- `related_frameworks`: a small map {package_name: short_description}
   for libraries the framework re-exports or directly wraps (e.g. Sanic
   for Torpedo, aiohttp for an API-client base). Use the same lower-case
   `package_name` form as in the curated frameworks map.
- `do`: 3-8 bullets on how to use the framework correctly.
- `dont`: 3-7 bullets on anti-patterns specific to this framework.
- `notes`: 3-6 high-signal facts agents need to *see* (response envelope
   shape, public API list, version pin, config schema, key reference paths).

If the input clearly doesn't support a section, return an empty list /
empty string for it rather than fabricating content. Padding is worse
than empty.

Output JSON only, conforming to the supplied schema. No prose.
"""


# Strict schema. additionalProperties=false keeps the model focused.
SYNTHESIS_SCHEMA: Dict[str, Any] = {
    "name": "ai_context_build_synthesis",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "framework_description": {
                "type": "string",
                "description": (
                    "2-4 sentences on what the framework is, what it wraps, and "
                    "the canonical bootstrap path. Plain text."
                ),
            },
            "related_frameworks": {
                "type": "array",
                "description": (
                    "Libraries the framework re-exports or directly wraps. "
                    "Each item must cite a path in the input."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "package_name": {"type": "string"},
                        "description": {"type": "string"},
                        "cites": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["package_name", "description", "cites"],
                },
            },
            "do": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "bullet": {"type": "string"},
                        "cites": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["bullet", "cites"],
                },
            },
            "dont": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "bullet": {"type": "string"},
                        "cites": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["bullet", "cites"],
                },
            },
            "notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "bullet": {"type": "string"},
                        "cites": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["bullet", "cites"],
                },
            },
        },
        "required": [
            "framework_description",
            "related_frameworks",
            "do",
            "dont",
            "notes",
        ],
    },
}


@dataclass
class SynthesisResult:
    """LLM-derived bullets, post-citation-verification."""

    framework_description: Optional[str] = None
    related_frameworks: Dict[str, str] = field(default_factory=dict)
    do: List[str] = field(default_factory=list)
    dont: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    used_llm: bool = False
    cache_hit: bool = False
    reason_skipped: Optional[str] = None
    items_dropped: int = 0  # citations didn't resolve
    model_used: Optional[str] = None


def synthesize(
    extraction: FrameworkExtraction,
    *,
    config: dict,
    model: Optional[str] = None,
) -> SynthesisResult:
    """Run the LLM synthesis pass. Always returns a :class:`SynthesisResult`."""
    result = SynthesisResult()

    if llm_disabled():
        result.reason_skipped = (
            "LLM disabled (CHANGEBRIEF_DISABLE_LLM or running under pytest)."
        )
        return result
    if not str(config.get("llm_api_key") or "").strip():
        result.reason_skipped = "llm_api_key not set; run `changebrief init`."
        return result

    model_name = (model or str(config.get("default_model") or "gpt-4o-mini")).strip()
    cache_key = _cache_key(extraction, model_name)

    cached = _cache_read(cache_key)
    if cached is not None:
        payload = cached
        result.cache_hit = True
    else:
        try:
            payload = _call_llm(extraction, config=config, model=model_name)
        except Exception as exc:  # noqa: BLE001 — explicit fail-open boundary
            LOG.warning("ai-context build synthesis failed: %s", exc)
            result.reason_skipped = f"LLM call failed: {exc}"
            return result
        if payload is None:
            result.reason_skipped = "LLM returned an unparseable payload."
            return result
        _cache_write(cache_key, payload)

    return _verify_and_build(payload, extraction, model_name, cache_hit=result.cache_hit)


# ---------------------------------------------------------------------------- LLM call


def _call_llm(
    extraction: FrameworkExtraction,
    *,
    config: dict,
    model: str,
) -> Optional[Dict[str, Any]]:
    from changebrief.core.llm._openai_tools import run_with_tools

    user = _build_user_prompt(extraction)
    text = run_with_tools(
        config=config,
        system=SYSTEM_PROMPT,
        user=user,
        tools=[],
        purpose="ai-context build (framework synthesis)",
        max_tool_rounds=1,
        temperature=0.2,
        model=model,
        response_format={"type": "json_schema", "json_schema": SYNTHESIS_SCHEMA},
        request_timeout=60.0,
    )
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _build_user_prompt(extraction: FrameworkExtraction) -> str:
    lines: List[str] = []
    lines.append(f"## Framework: `{extraction.package_name}`")
    if extraction.summary:
        lines.append(f"_summary_: {extraction.summary}")
    lines.append(f"_repo_root_: `{extraction.repo_root}`")
    lines.append(f"_primary_language_: {extraction.primary_language}")
    lines.append("")

    lines.append("## Verified facts")
    for fact in extraction.framework_facts:
        lines.append(f"- {fact}")
    lines.append("")

    if extraction.public_api:
        lines.append("## Public API symbols")
        for sym in extraction.public_api[:30]:
            lines.append(f"- `{sym.name}` ({sym.kind}) — defined near `{sym.source}`")
        lines.append("")

    if extraction.exceptions:
        lines.append("## Exception classes")
        for exc in extraction.exceptions[:30]:
            base_str = ", ".join(exc.bases) if exc.bases else "—"
            lines.append(f"- `{exc.name}` (bases: {base_str}) — `{exc.source}`")
        lines.append("")

    if extraction.decorators:
        lines.append("## Decorator candidates (heuristic; confirm against docs/source)")
        for dec in extraction.decorators[:15]:
            lines.append(f"- `{dec.name}` — `{dec.source}`")
        lines.append("")

    if extraction.notable_dirs:
        lines.append("## Notable subdirectories")
        for nd in extraction.notable_dirs:
            lines.append(f"- `{nd.rel_path}` — {nd.description}")
        lines.append("")

    if extraction.config_keys:
        lines.append("## config.json top-level keys")
        lines.append("- " + ", ".join(f"`{k}`" for k in extraction.config_keys))
        lines.append("")

    if extraction.readme_excerpt:
        lines.append("## README excerpt (`README.md`)")
        lines.append("```")
        lines.append(extraction.readme_excerpt)
        lines.append("```")
        lines.append("")

    for rel, text in extraction.doc_excerpts:
        lines.append(f"## Docs excerpt (`{rel}`)")
        lines.append("```")
        lines.append(text)
        lines.append("```")
        lines.append("")

    for ex in extraction.examples:
        lines.append(f"## Example (`{ex.rel_path}`)")
        lines.append("```")
        lines.append(ex.content)
        lines.append("```")
        lines.append("")

    lines.append("## Citation rules")
    lines.append("- Only cite paths from this list (anything else will be dropped):")
    for path in extraction.sample_paths:
        lines.append(f"  - `{path}`")
    lines.append("")
    lines.append(
        "Return JSON conforming to the schema. Empty arrays are fine when the "
        "input doesn't support a section."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------- verify


def _verify_and_build(
    payload: Dict[str, Any],
    extraction: FrameworkExtraction,
    model_name: str,
    *,
    cache_hit: bool,
) -> SynthesisResult:
    """Validate payload citations against ``extraction.sample_paths`` and assemble."""
    valid_paths = set(extraction.sample_paths)
    repo_root = Path(extraction.repo_root)

    desc_raw = str(payload.get("framework_description") or "").strip()
    description = _sanitize_description(desc_raw)

    related_raw = payload.get("related_frameworks") or []
    related_kept: Dict[str, str] = {}
    dropped = 0
    if isinstance(related_raw, list):
        for item in related_raw:
            if not isinstance(item, dict):
                dropped += 1
                continue
            pkg = str(item.get("package_name") or "").strip().lower()
            desc = str(item.get("description") or "").strip()
            cites = item.get("cites") or []
            if not pkg or not desc:
                dropped += 1
                continue
            if not _cites_resolve(cites, valid_paths, repo_root):
                dropped += 1
                continue
            if pkg in related_kept:
                continue
            related_kept[pkg] = desc
            if len(related_kept) >= MAX_RELATED_FRAMEWORKS:
                break

    do_kept, do_dropped = _verified_bullets(
        payload.get("do") or [], valid_paths, repo_root, MAX_DO
    )
    dont_kept, dont_dropped = _verified_bullets(
        payload.get("dont") or [], valid_paths, repo_root, MAX_DONT
    )
    notes_kept, notes_dropped = _verified_bullets(
        payload.get("notes") or [], valid_paths, repo_root, MAX_NOTES
    )

    return SynthesisResult(
        framework_description=description or None,
        related_frameworks=related_kept,
        do=do_kept,
        dont=dont_kept,
        notes=notes_kept,
        used_llm=True,
        cache_hit=cache_hit,
        items_dropped=dropped + do_dropped + dont_dropped + notes_dropped,
        model_used=model_name,
    )


def _verified_bullets(
    items: List[Any],
    valid_paths: Set[str],
    repo_root: Path,
    cap: int,
) -> Tuple[List[str], int]:
    kept: List[str] = []
    dropped = 0
    for raw in items:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        bullet = str(raw.get("bullet") or "").strip()
        cites = raw.get("cites") or []
        if not bullet:
            dropped += 1
            continue
        if not _cites_resolve(cites, valid_paths, repo_root):
            dropped += 1
            continue
        bullet = _strip_absolute_paths(bullet)
        # Preserve at least one explicit citation in the rendered bullet so
        # downstream consumers (notably `init --enrich-deps`) can enforce
        # citation gating just like the repo-level enricher does.
        cite_first = str(cites[0]).strip() if isinstance(cites, list) and cites else ""
        evidence = f" _(evidence: `{cite_first}`)_" if cite_first else ""
        kept.append(bullet + evidence)
        if len(kept) >= cap:
            break
    return kept, dropped


_ABS_PATH_RE = re.compile(r"(?<![`\\w])/(?:Users|home|opt|var|private|Volumes)/[^\\s)`\\]]{2,200}")


def _strip_absolute_paths(text: str) -> str:
    """Remove absolute local filesystem paths from LLM bullet text.

    These often leak into framework notes (e.g. `/opt/...`) and reduce portability.
    We keep repo-relative cites separately via `_(evidence: ...)`.
    """
    s = (text or "").strip()
    if not s:
        return s
    return _ABS_PATH_RE.sub("`<path>`", s)


def _cites_resolve(cites: Any, valid_paths: Set[str], repo_root: Path) -> bool:
    """At least one cite must be in the allowed set AND exist on disk.

    We accept matches both exact-string and as a resolved file under
    ``repo_root`` so the model can cite `package/__init__.py` even when the
    extractor only listed `package_dir/__init__.py`.
    """
    if not isinstance(cites, list) or not cites:
        return False
    for raw in cites:
        cand = str(raw or "").strip()
        if not cand:
            continue
        if cand in valid_paths:
            return True
        try:
            resolved = (repo_root / cand).resolve()
            resolved.relative_to(repo_root.resolve())
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return True
    return False


def _sanitize_description(raw: str) -> str:
    """Strip trailing whitespace, keep newlines so block-scalar YAML stays readable."""
    return "\n".join(line.rstrip() for line in raw.splitlines() if line.strip()) if "\n" in raw else raw.strip()


# ---------------------------------------------------------------------------- cache


def _cache_key(extraction: FrameworkExtraction, model: str) -> str:
    h = hashlib.sha256()
    h.update(f"v={PROMPT_VERSION}\n".encode())
    h.update(f"model={model}\n".encode())
    h.update(f"pkg={extraction.package_name}\n".encode())
    h.update(f"root={Path(extraction.repo_root).resolve()}\n".encode())
    repo = Path(extraction.repo_root)
    for path in extraction.sample_paths:
        full = repo / path
        h.update(path.encode("utf-8", errors="replace") + b"\n")
        try:
            h.update(hashlib.sha256(full.read_bytes()).digest() if full.is_file() else b"missing")
        except OSError:
            h.update(b"missing")
    return h.hexdigest()[:32]


def _cache_path(key: str) -> Path:
    return get_config_dir() / "cache" / CACHE_DIR_NAME / f"{key}.json"


def _cache_read(key: str) -> Optional[Dict[str, Any]]:
    path = _cache_path(key)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if time.time() - float(raw.get("created_at") or 0) > CACHE_TTL_S:
        return None
    data = raw.get("data")
    return data if isinstance(data, dict) else None


def _cache_write(key: str, data: Dict[str, Any]) -> None:
    path = _cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"created_at": time.time(), "data": data}
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
