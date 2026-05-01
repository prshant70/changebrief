from __future__ import annotations

from changebrief.core.ai_context.models import AIContextSection
from changebrief.core.ai_context import enricher as enricher_mod
from changebrief.core.ai_context.enricher import _merge_into_section


def test_merge_into_section_inserts_and_caps() -> None:
    sections = [
        AIContextSection(title="Do", bullets=["a", "b", "c", "d"]),
        AIContextSection(title="References", bullets=["x"]),
    ]
    out = _merge_into_section(
        sections,
        title="Do",
        additions=["new1", "new2", "b"],
        cap=5,
        insert_after=2,
    )
    do = next(s for s in out if s.title == "Do")
    assert do.bullets == ["a", "b", "new1", "new2", "c"]


def test_verified_items_drops_unsafe_paths(tmp_path) -> None:
    repo_root = tmp_path
    (repo_root / "ok.py").write_text("x=1", encoding="utf-8")
    kept, dropped = enricher_mod._verified_items(
        repo_root,
        [
            {"bullet": "ok", "evidence_path": "ok.py"},
            {"bullet": "nope", "evidence_path": "../etc/passwd"},
            {"bullet": "missing", "evidence_path": "missing.py"},
        ],
        text_field="bullet",
        cap=10,
    )
    assert kept == [("ok", "ok.py")]
    assert dropped == 2


def test_enricher_cache_roundtrip(monkeypatch, tmp_path) -> None:
    # Ensure cache uses a temp directory rather than ~/.changebrief.
    monkeypatch.setattr(enricher_mod, "get_config_dir", lambda: tmp_path / ".changebrief")
    key = "k123"
    payload = {"polished_overview": "x", "inferred_conventions": [], "gotchas": [], "do": [], "dont": [], "notes": []}
    enricher_mod._cache_write(key, payload)
    got = enricher_mod._cache_read(key)
    assert got == payload

