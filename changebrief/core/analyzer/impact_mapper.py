"""LLM-driven impact mapping using language-agnostic tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from changebrief.core.analyzer.change_analyzer import ChangeSummary
from changebrief.core.tools.code_tools import CodeTools
from changebrief.core.llm.guard import llm_disabled


@dataclass
class ImpactSummary:
    endpoints: List[str]
    mapping: Dict[str, str]  # function -> "METHOD /path"


def map_impact(
    change_summary: ChangeSummary,
    code_tools: CodeTools,
) -> ImpactSummary:
    """
    Map changes to impacted user-facing endpoints (HTTP, RPC, CLI, etc.)

    Language/framework agnostic v1:
    - If API key missing: return empty impact (still useful with risk + plan).
    - If present: ask LLM to inspect diff + selectively read/search files using tools.
    """
    cfg = code_tools.config
    if llm_disabled() or not str(cfg.get("llm_api_key") or "").strip():
        return ImpactSummary(endpoints=[], mapping={})

    import json

    from changebrief.core.llm._openai_tools import ToolSpec, run_with_tools

    tools = [
        ToolSpec(
            name="get_diff",
            description="Return git diff (base...feature).",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: code_tools.get_diff(),
        ),
        ToolSpec(
            name="search_code",
            description="Substring search across repo; returns matching file paths.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=lambda query: code_tools.search_code(query),
        ),
        ToolSpec(
            name="read_file",
            description="Read file content (relative to repo root).",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=lambda path: code_tools.read_file(path),
        ),
        ToolSpec(
            name="list_files",
            description="List repository files (optionally filtered by suffix).",
            parameters={
                "type": "object",
                "properties": {
                    "suffix": {"type": "string", "description": "e.g. '.py' or '.ts' or empty"},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
            handler=lambda suffix=None, limit=200: code_tools.list_files(suffix=suffix, limit=limit),
        ),
        ToolSpec(
            name="git_show",
            description="Read a file as it exists at a git ref, for context.",
            parameters={
                "type": "object",
                "properties": {"ref": {"type": "string"}, "path": {"type": "string"}},
                "required": ["ref", "path"],
            },
            handler=lambda ref, path: code_tools.git_show(ref=ref, path=path),
        ),
    ]

    system = (
        "You map code changes to impacted user-facing endpoints (HTTP routes, RPC methods, CLI commands). "
        "You MUST use the tools to inspect the repo and diff. "
        "Return ONLY JSON: {\"endpoints\": [\"...\"], \"mapping\": {\"change\": \"endpoint\"}}. "
        "Use stable, human-readable endpoint strings (e.g. 'POST /users', 'GET /v1/orders', 'rpc:CreateOrder', 'cli:changebrief validate'). "
        "If you cannot confidently infer endpoints, return an empty list and empty mapping."
    )
    user = (
        "Changed files:\n"
        + "\n".join(f"- {f}" for f in change_summary.files[:50])
        + "\n\nUse get_diff and read/search as needed to infer impacted endpoints."
    )

    text = run_with_tools(
        config=cfg,
        system=system,
        user=user,
        tools=tools,
        purpose="map changes to impacted endpoints",
        max_tool_rounds=5,
        temperature=0.2,
    )

    try:
        data = json.loads(text)
        eps = data.get("endpoints") or []
        mp = data.get("mapping") or {}
        if isinstance(eps, list) and isinstance(mp, dict):
            eps2 = [str(x).strip() for x in eps if str(x).strip()]
            mp2 = {str(k): str(v) for k, v in mp.items()}
            # de-dupe endpoints
            seen: set[str] = set()
            uniq_eps: list[str] = []
            for e in eps2:
                if e not in seen:
                    seen.add(e)
                    uniq_eps.append(e)
            return ImpactSummary(endpoints=uniq_eps[:25], mapping=mp2)
    except Exception:
        pass

    return ImpactSummary(endpoints=[], mapping={})

