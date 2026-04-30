"""Optional LLM enrichment for ai-context generation.

Design rules (safety first):

* The deterministic baseline always runs. The LLM only **augments** bounded
  fields — never the dependency list, scripts, or structure where we have
  ground truth.
* Output is enforced via JSON Schema (``response_format=json_schema``),
  same pattern as :mod:`changebrief.core.llm.validation_planner`.
* Every LLM-suggested bullet must cite an ``evidence_path``. We verify that
  path resolves to a real file inside the repo and drop the bullet if it
  doesn't. This kills the most common hallucination class for free.
* All prompt content is passed through :mod:`changebrief.core.redaction`.
* Cached on (repo + model + prompt version + sampled file digests) so
  unchanged repos are free to re-enrich.
* Fail-open: any error returns the unmodified ``AIContext`` so the command
  never breaks because of LLM issues.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from changebrief.core.ai_context.models import AIContext, AIContextSection, RepoContext
from changebrief.core.llm.guard import llm_disabled
from changebrief.utils.paths import get_config_dir


LOG = logging.getLogger("changebrief")


PROMPT_VERSION = "1"
CACHE_DIR_NAME = "ai-context-llm"
CACHE_TTL_S = 7 * 24 * 60 * 60  # 7 days
SAMPLE_FILE_HEAD_BYTES = 1500
SAMPLE_FILE_TAIL_BYTES = 500
MAX_SAMPLED_FILES = 5
MAX_CONVENTIONS = 6
MAX_GOTCHAS = 4
MAX_DO = 5
MAX_DONT = 4
MAX_NOTES = 5

# Final section caps (must stay aligned with composer output expectations).
_FINAL_DO_CAP = 10
_FINAL_DONT_CAP = 8
_FINAL_NOTES_CAP = 12


SYSTEM_PROMPT = """\
You are reading a real codebase so an AI coding agent can be effective in it.

You will be given:
- Verified project facts (language, frameworks, run scripts) — already produced
  by a deterministic scanner. Do NOT restate these.
- A README excerpt.
- 3-5 representative source files with their relative paths.

Your job: produce concise, repo-specific guidance.

CRITICAL RULES (must follow):
1. Every observation MUST cite an `evidence_path` that exactly matches a file
   path you saw in the input. No invented paths.
2. Do NOT invent classes, functions, modules, or commands not present in the
   provided files.
3. Avoid restating obvious facts the deterministic scanner already covered
   (language, framework names, package manager). Add NEW signal.
4. Prefer specific, behavioral observations and service invariants:
     "errors are wrapped in ChangeBriefError subclasses (see core/exceptions.py)"
   over generic platitudes:
     "write clean, maintainable code".
5. Focus on what prevents real mistakes:
   - event/payload contracts, idempotency, retries/DLQ, logging requirements,
     auth middleware expectations, persistence conventions.
   - "golden path" pointers: where to add a new handler / route / manager.
6. If a section yields nothing high-quality, return an empty list. Padding
   with weak items is worse than empty.
7. Output JSON only, conforming to the supplied schema. No prose, no markdown.
"""


# Strict schema. additionalProperties=false keeps the model from drifting.
ENRICHMENT_SCHEMA: Dict[str, Any] = {
    "name": "ai_context_enrichment",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "polished_overview": {
                "type": "string",
                "description": "1-2 sentences describing what the project does, in concrete terms.",
            },
            "inferred_conventions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "observation": {"type": "string"},
                        "evidence_path": {"type": "string"},
                    },
                    "required": ["observation", "evidence_path"],
                },
            },
            "gotchas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "description": {"type": "string"},
                        "evidence_path": {"type": "string"},
                    },
                    "required": ["description", "evidence_path"],
                },
            },
            "do": {
                "type": "array",
                "description": "Small set of repo-specific Do bullets (avoid framework manuals).",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "bullet": {"type": "string"},
                        "evidence_path": {"type": "string"},
                    },
                    "required": ["bullet", "evidence_path"],
                },
            },
            "dont": {
                "type": "array",
                "description": "Small set of repo-specific Don't bullets (high-risk footguns only).",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "bullet": {"type": "string"},
                        "evidence_path": {"type": "string"},
                    },
                    "required": ["bullet", "evidence_path"],
                },
            },
            "notes": {
                "type": "array",
                "description": "High-signal repo-specific notes (contracts, envelopes, pins) with citations.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "bullet": {"type": "string"},
                        "evidence_path": {"type": "string"},
                    },
                    "required": ["bullet", "evidence_path"],
                },
            },
        },
        "required": [
            "polished_overview",
            "inferred_conventions",
            "gotchas",
            "do",
            "dont",
            "notes",
        ],
    },
}


@dataclass
class EnrichmentResult:
    """What the enricher did, for logging / `--enrich --verbose`."""

    used_llm: bool = False
    reason_skipped: Optional[str] = None
    cache_hit: bool = False
    convs_kept: int = 0
    convs_dropped: int = 0
    gotchas_kept: int = 0
    gotchas_dropped: int = 0
    sampled_files: List[str] = field(default_factory=list)


def enrich(
    ai_ctx: AIContext,
    repo_ctx: RepoContext,
    *,
    config: dict,
    model: Optional[str] = None,
) -> Tuple[AIContext, EnrichmentResult]:
    """Run optional LLM enrichment. Always returns a usable AIContext.

    Falls back to the input ``ai_ctx`` unchanged on any failure or when LLM is
    disabled. The :class:`EnrichmentResult` reports what happened.
    """
    result = EnrichmentResult()

    if llm_disabled():
        result.reason_skipped = "LLM disabled (CHANGEBRIEF_DISABLE_LLM or running under pytest)."
        return ai_ctx, result
    if not str(config.get("llm_api_key") or "").strip():
        result.reason_skipped = "llm_api_key not set; run `changebrief init`."
        return ai_ctx, result

    repo_root = Path(repo_ctx.root)
    sampled = _sample_files(repo_root, repo_ctx)
    result.sampled_files = [str(p.relative_to(repo_root)) for p, _ in sampled]
    if not sampled:
        result.reason_skipped = "No sampled source files found to enrich from."
        return ai_ctx, result

    model_name = (model or str(config.get("default_model") or "gpt-4o-mini")).strip()
    cache_key = _cache_key(repo_root, model_name, sampled)
    cached = _cache_read(cache_key)
    if cached is not None:
        result.cache_hit = True
        payload = cached
    else:
        try:
            payload = _call_llm(repo_ctx, sampled, config=config, model=model_name)
        except Exception as exc:  # noqa: BLE001 — explicit fail-open boundary
            LOG.warning("ai-context enrichment failed: %s", exc)
            result.reason_skipped = f"LLM call failed: {exc}"
            return ai_ctx, result
        if payload is None:
            result.reason_skipped = "LLM returned an unparseable payload."
            return ai_ctx, result
        _cache_write(cache_key, payload)

    enriched, kept, dropped = _merge(ai_ctx, repo_root, payload)
    result.used_llm = True
    result.convs_kept = kept[0]
    result.convs_dropped = dropped[0]
    result.gotchas_kept = kept[1]
    result.gotchas_dropped = dropped[1]
    return enriched, result


# ---------------------------------------------------------------------------- merge


def _merge(
    ai_ctx: AIContext,
    repo_root: Path,
    payload: Dict[str, Any],
) -> Tuple[AIContext, Tuple[int, int], Tuple[int, int]]:
    overview = ai_ctx.overview
    new_overview = (payload.get("polished_overview") or "").strip()
    if new_overview:
        overview = new_overview

    conv_kept, conv_dropped = _verified_items(
        repo_root,
        payload.get("inferred_conventions") or [],
        text_field="observation",
        cap=MAX_CONVENTIONS,
    )
    goth_kept, goth_dropped = _verified_items(
        repo_root,
        payload.get("gotchas") or [],
        text_field="description",
        cap=MAX_GOTCHAS,
    )

    sections = list(ai_ctx.sections)

    # Insert "Inferred conventions" right after "Conventions (observed)" if present.
    if conv_kept:
        section = AIContextSection(
            title="Inferred conventions (LLM)",
            bullets=[f"{obs} _(evidence: `{path}`)_" for obs, path in conv_kept],
        )
        sections = _insert_after(sections, after_title="Conventions (observed)", new_section=section)

    if goth_kept:
        section = AIContextSection(
            title="Gotchas (LLM)",
            bullets=[f"{txt} _(evidence: `{path}`)_" for txt, path in goth_kept],
        )
        sections = _insert_before(sections, before_title="References", new_section=section)

    do_kept, do_dropped = _verified_items(
        repo_root,
        payload.get("do") or [],
        text_field="bullet",
        cap=MAX_DO,
    )
    dont_kept, dont_dropped = _verified_items(
        repo_root,
        payload.get("dont") or [],
        text_field="bullet",
        cap=MAX_DONT,
    )
    notes_kept, notes_dropped = _verified_items(
        repo_root,
        payload.get("notes") or [],
        text_field="bullet",
        cap=MAX_NOTES,
    )

    if do_kept:
        sections = _merge_into_section(
            sections,
            title="Do",
            additions=[f"{txt} _(evidence: `{path}`)_" for txt, path in do_kept],
            cap=_FINAL_DO_CAP,
            insert_after=3,  # keep deterministic top bullets first
        )
    if dont_kept:
        sections = _merge_into_section(
            sections,
            title="Don't",
            additions=[f"{txt} _(evidence: `{path}`)_" for txt, path in dont_kept],
            cap=_FINAL_DONT_CAP,
            insert_after=2,
        )
    if notes_kept:
        sections = _merge_into_section(
            sections,
            title="Notes",
            additions=[f"{txt} _(evidence: `{path}`)_" for txt, path in notes_kept],
            cap=_FINAL_NOTES_CAP,
            insert_after=0,
        )

    enriched = AIContext(
        project_name=ai_ctx.project_name,
        overview=overview,
        sections=sections,
    )
    # Report only the original convention/gotcha counts (keeps CLI output stable).
    _ = (do_dropped, dont_dropped, notes_dropped)
    return enriched, (len(conv_kept), len(goth_kept)), (conv_dropped, goth_dropped)


def _verified_items(
    repo_root: Path,
    items: List[Any],
    *,
    text_field: str,
    cap: int,
) -> Tuple[List[Tuple[str, str]], int]:
    kept: List[Tuple[str, str]] = []
    dropped = 0
    for raw in items:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        text = str(raw.get(text_field) or "").strip()
        path = str(raw.get("evidence_path") or "").strip()
        if not text or not path:
            dropped += 1
            continue
        if not _path_is_safe_and_exists(repo_root, path):
            dropped += 1
            continue
        kept.append((text, path))
        if len(kept) >= cap:
            break
    return kept, dropped


def _path_is_safe_and_exists(repo_root: Path, raw: str) -> bool:
    """Resolve ``raw`` under ``repo_root`` and ensure it stays inside it."""
    try:
        resolved = (repo_root / raw).resolve(strict=False)
        repo_resolved = repo_root.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    try:
        resolved.relative_to(repo_resolved)
    except ValueError:
        return False
    return resolved.exists()


def _insert_after(
    sections: List[AIContextSection],
    *,
    after_title: str,
    new_section: AIContextSection,
) -> List[AIContextSection]:
    out = list(sections)
    for idx, sec in enumerate(out):
        if sec.title == after_title:
            out.insert(idx + 1, new_section)
            return out
    out.append(new_section)
    return out


def _insert_before(
    sections: List[AIContextSection],
    *,
    before_title: str,
    new_section: AIContextSection,
) -> List[AIContextSection]:
    out = list(sections)
    for idx, sec in enumerate(out):
        if sec.title == before_title:
            out.insert(idx, new_section)
            return out
    out.append(new_section)
    return out


def _merge_into_section(
    sections: List[AIContextSection],
    *,
    title: str,
    additions: List[str],
    cap: int,
    insert_after: int,
) -> List[AIContextSection]:
    """Merge bullets into an existing section title, with dedupe + cap."""
    out = list(sections)
    for idx, sec in enumerate(out):
        if sec.title != title:
            continue
        existing = list(sec.bullets or [])
        insert_at = max(0, min(len(existing), int(insert_after)))
        merged: List[str] = []
        merged.extend(existing[:insert_at])
        for item in additions:
            if item and item not in merged and item not in existing:
                merged.append(item)
        merged.extend(existing[insert_at:])
        sec2 = AIContextSection(title=sec.title, bullets=merged[: max(0, int(cap))], paragraphs=list(sec.paragraphs))
        out[idx] = sec2
        return out
    return out


# ---------------------------------------------------------------------------- LLM call


def _call_llm(
    repo_ctx: RepoContext,
    sampled: List[Tuple[Path, str]],
    *,
    config: dict,
    model: str,
) -> Optional[Dict[str, Any]]:
    from changebrief.core.llm._openai_tools import run_with_tools

    user = _build_user_prompt(repo_ctx, sampled)
    text = run_with_tools(
        config=config,
        system=SYSTEM_PROMPT,
        user=user,
        tools=[],
        purpose="ai-context enrichment (structured JSON)",
        max_tool_rounds=1,
        temperature=0.1,
        model=model,
        response_format={"type": "json_schema", "json_schema": ENRICHMENT_SCHEMA},
        request_timeout=45.0,
    )
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _build_user_prompt(repo_ctx: RepoContext, sampled: List[Tuple[Path, str]]) -> str:
    lines: List[str] = []
    lines.append("## Verified project facts (from deterministic scanner)")
    lines.append(f"- Project name: `{repo_ctx.project_name}`")
    lines.append(f"- Primary language: {repo_ctx.primary_language}")
    fws: list[str] = []
    for p in repo_ctx.profiles:
        for ev in p.frameworks:
            fws.append(ev.fact)
    if fws:
        lines.append("- Frameworks/libs: " + ", ".join(sorted(set(fws))[:12]))
    if repo_ctx.top_directories:
        dirs = ", ".join(d.name for d in repo_ctx.top_directories[:10])
        lines.append(f"- Top-level dirs: {dirs}")

    repo_root = Path(repo_ctx.root)
    readme = _read_readme(repo_root)
    if readme:
        lines.append("")
        lines.append("## README excerpt")
        lines.append("```")
        lines.append(readme)
        lines.append("```")

    lines.append("")
    lines.append("## Sampled source files")
    for path, content in sampled:
        rel = str(path.relative_to(repo_root))
        lines.append("")
        lines.append(f"### File: `{rel}`")
        lines.append("```")
        lines.append(content)
        lines.append("```")

    lines.append("")
    lines.append(
        "Return JSON conforming to the schema. Cite an `evidence_path` for every "
        "convention and gotcha — paths must be ones that appear above."
    )
    return "\n".join(lines)


def _read_readme(repo_root: Path, *, max_chars: int = 2000) -> Optional[str]:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        p = repo_root / name
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8", errors="replace")[:max_chars]
            except OSError:
                return None
    return None


# ---------------------------------------------------------------------------- sampling


def _sample_files(repo_root: Path, repo_ctx: RepoContext) -> List[Tuple[Path, str]]:
    """Pick up to ``MAX_SAMPLED_FILES`` representative source files."""
    candidates = _git_recently_changed_files(repo_root)
    if not candidates:
        candidates = _largest_source_files(repo_root, repo_ctx)

    out: List[Tuple[Path, str]] = []
    for rel in candidates:
        if len(out) >= MAX_SAMPLED_FILES:
            break
        path = (repo_root / rel).resolve()
        try:
            path.relative_to(repo_root.resolve())
        except ValueError:
            continue
        if not path.is_file():
            continue
        if path.stat().st_size > 200_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        snippet = _head_tail(text)
        out.append((path, snippet))
    return out


def _head_tail(text: str) -> str:
    if len(text) <= SAMPLE_FILE_HEAD_BYTES + SAMPLE_FILE_TAIL_BYTES:
        return text
    return (
        text[:SAMPLE_FILE_HEAD_BYTES]
        + f"\n... [{len(text) - SAMPLE_FILE_HEAD_BYTES - SAMPLE_FILE_TAIL_BYTES} bytes elided] ...\n"
        + text[-SAMPLE_FILE_TAIL_BYTES:]
    )


def _git_recently_changed_files(repo_root: Path) -> List[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-n", "80", "--pretty=format:", "--name-only"],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    counts: Dict[str, int] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if not _is_sample_worthy(line):
            continue
        counts[line] = counts.get(line, 0) + 1
    return [p for p, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _largest_source_files(repo_root: Path, repo_ctx: RepoContext) -> List[str]:
    candidates: List[Tuple[int, str]] = []
    primary_profile = next(
        (p for p in repo_ctx.profiles if p.language == repo_ctx.primary_language),
        None,
    )
    if not primary_profile:
        return []
    extensions = {
        "python": (".py",),
        "javascript": (".js", ".jsx", ".mjs", ".cjs"),
        "typescript": (".ts", ".tsx"),
        "go": (".go",),
        "rust": (".rs",),
        "java": (".java", ".kt"),
        "ruby": (".rb",),
    }.get(primary_profile.language, ())
    search_dirs = [repo_root / d for d in primary_profile.source_dirs if (repo_root / d).is_dir()]
    if not search_dirs:
        search_dirs = [repo_root]
    for sd in search_dirs:
        for path in sd.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in extensions:
                continue
            if not _is_sample_worthy(str(path.relative_to(repo_root))):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            candidates.append((size, str(path.relative_to(repo_root))))
    candidates.sort(key=lambda kv: -kv[0])
    return [rel for _, rel in candidates[: MAX_SAMPLED_FILES * 2]]


def _is_sample_worthy(rel_path: str) -> bool:
    parts = rel_path.split("/")
    skip_tokens = {
        ".git", "node_modules", ".venv", "venv", "__pycache__", ".tox",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
        "target", ".next", ".cache", "vendor",
    }
    if any(p in skip_tokens for p in parts):
        return False
    if any(p.endswith(".egg-info") for p in parts):
        return False
    # Skip lockfiles, generated artifacts.
    name = parts[-1]
    if name in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock", "go.sum"}:
        return False
    return True


# ---------------------------------------------------------------------------- cache


def _cache_key(repo_root: Path, model: str, sampled: List[Tuple[Path, str]]) -> str:
    h = hashlib.sha256()
    h.update(f"v={PROMPT_VERSION}\n".encode())
    h.update(f"model={model}\n".encode())
    h.update(f"root={repo_root.resolve()}\n".encode())
    for path, content in sampled:
        h.update(f"path={path}\n".encode())
        h.update(hashlib.sha256(content.encode("utf-8", errors="replace")).digest())
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
