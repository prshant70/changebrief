"""Small OpenAI helper for structured tool-calling (bounded loop).

Notes for reviewers:
- All content sent to the model is passed through :mod:`changebrief.core.redaction`
  so secrets and PII never leave the user's machine.
- Requests are bounded by ``request_timeout`` to avoid CLI hangs.
- ``response_format`` lets callers enforce a JSON Schema, which we use for the
  validation planner so output is parsed by schema rather than emoji prefixes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from changebrief.core.exceptions import ConfigError
from changebrief.core.redaction import redact


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]


DEFAULT_REQUEST_TIMEOUT_S = 60.0


def _require_api_key(config: dict) -> str:
    key = str(config.get("llm_api_key") or "").strip()
    if not key:
        raise ConfigError("llm_api_key is empty; run `changebrief init` to set it.")
    return key


def _client(config: dict, *, timeout: float = DEFAULT_REQUEST_TIMEOUT_S):
    _require_api_key(config)
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ConfigError("Missing dependency: install the 'openai' package.") from exc
    return OpenAI(api_key=str(config.get("llm_api_key") or "").strip(), timeout=timeout)


def _redact_str(value: str, *, enabled: bool) -> str:
    if not enabled:
        return value
    return redact(value)


def run_with_tools(
    *,
    config: dict,
    system: str,
    user: str,
    tools: List[ToolSpec],
    purpose: Optional[str] = None,
    log_tool_calls: bool = True,
    max_tool_rounds: int = 4,
    model: Optional[str] = None,
    temperature: float = 0.2,
    response_format: Optional[Dict[str, Any]] = None,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
    redact_io: bool = True,
) -> str:
    """
    Run a bounded tool-calling loop and return the assistant's final text output.

    All inbound prompts and outbound tool results are redacted by default.
    """
    log = logging.getLogger("changebrief")
    client = _client(config, timeout=request_timeout)
    model_name = model or str(config.get("default_model") or "gpt-4o-mini").strip()
    if purpose:
        log.info("LLM call: %s (model=%s, temp=%.2f)", purpose, model_name, float(temperature))

    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]
    handlers: Dict[str, Callable[..., Any]] = {t.name: t.handler for t in tools}

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _redact_str(system, enabled=redact_io)},
        {"role": "user", "content": _redact_str(user, enabled=redact_io)},
    ]

    def _create(*, tool_choice: Optional[str]) -> Any:
        kwargs: Dict[str, Any] = {
            "model": model_name,
            "temperature": temperature,
            "messages": messages,
            "timeout": request_timeout,
        }
        if openai_tools:
            kwargs["tools"] = openai_tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if response_format is not None:
            kwargs["response_format"] = response_format
        return client.chat.completions.create(**kwargs)

    for _ in range(max_tool_rounds):
        resp = _create(tool_choice="auto" if openai_tools else None)
        msg = resp.choices[0].message

        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return (msg.content or "").strip()

        messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ],
            },
        )

        for tc in tool_calls:
            name = tc.function.name
            raw_args = tc.function.arguments or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}
            if log_tool_calls:
                arg_preview = raw_args
                if len(arg_preview) > 300:
                    arg_preview = arg_preview[:300] + "…"
                log.info("Tool call: %s %s", name, arg_preview)
            handler = handlers.get(name)
            if handler is None:
                out = f"ERROR: unknown tool {name!r}"
            else:
                try:
                    out = handler(**args)
                except Exception as exc:  # keep loop deterministic-ish
                    out = f"ERROR: tool failed: {exc}"
            tool_payload = json.dumps(out, ensure_ascii=False)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _redact_str(tool_payload, enabled=redact_io),
                },
            )

    # If the model keeps calling tools, force a final response.
    messages.append(
        {
            "role": "user",
            "content": "Stop calling tools. Provide the final answer now.",
        },
    )
    resp = _create(tool_choice="none" if openai_tools else None)
    return (resp.choices[0].message.content or "").strip()
