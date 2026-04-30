from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from changebrief.core.llm import _openai_tools as mod


class _FakeChat:
    def __init__(self, resp):
        self.completions = SimpleNamespace(create=lambda **_: resp)


class _FakeClient:
    def __init__(self, resp):
        self.chat = _FakeChat(resp)


def test_openai_usage_is_logged_to_calllog(tmp_path: Path, monkeypatch) -> None:
    # Redirect ~/.changebrief to tmp HOME via session fixture; now point config dir there.
    # (tests/conftest.py already isolates HOME)
    usage = SimpleNamespace(prompt_tokens=12, completion_tokens=34, total_tokens=46)
    msg = SimpleNamespace(content="ok", tool_calls=None)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)

    monkeypatch.setattr(mod, "_client", lambda *a, **k: _FakeClient(resp))

    out = mod.run_with_tools(
        config={"llm_api_key": "sk-test", "default_model": "gpt-4o-mini"},
        system="x",
        user="y",
        tools=[],
        purpose="unit test",
        max_tool_rounds=1,
        temperature=0.0,
        redact_io=False,
    )
    assert out == "ok"

    calllog = Path.home() / ".changebrief" / "llm-calllog.jsonl"
    assert calllog.exists()
    lines = calllog.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "calllog must contain at least one entry"
    last = json.loads(lines[-1])
    assert last["provider"] == "openai"
    assert last["purpose"] == "unit test"
    assert last["input_tokens"] == 12
    assert last["output_tokens"] == 34
    assert last["total_tokens"] == 46

