"""Tests for the risk classifier — focuses on false-positive resistance."""

from __future__ import annotations

from changebrief.core.analyzer.change_analyzer import ChangeSummary
from changebrief.core.analyzer.risk_classifier import classify_risk


def _diff(file: str, *, added=(), removed=()) -> str:
    """Build a minimal unified diff for a single file."""
    lines = [
        f"diff --git a/{file} b/{file}",
        f"--- a/{file}",
        f"+++ b/{file}",
        "@@ -1,1 +1,1 @@",
    ]
    for line in removed:
        lines.append("-" + line)
    for line in added:
        lines.append("+" + line)
    return "\n".join(lines) + "\n"


# -- false positives the old classifier produced -----------------------------


def test_substring_db_does_not_falsely_trigger_persistence() -> None:
    cs = ChangeSummary(
        files=["app.py"],
        functions=[],
        diff_text=_diff(
            "app.py",
            added=[
                "# This comment doubts the result",
                "stub_value = 1",
                "audit_log = 'breadcrumb'",
                "msg = 'we have lambda x: x'",
            ],
        ),
    )
    risk = classify_risk(cs)
    assert "data persistence change" not in risk.types


def test_substring_http_in_comment_does_not_trigger_external_call() -> None:
    cs = ChangeSummary(
        files=["app.py"],
        functions=[],
        diff_text=_diff(
            "app.py",
            added=[
                "# See http://example.com/docs for details",
                "# https://example.com explains this",
                "x = 1",
            ],
        ),
    )
    risk = classify_risk(cs)
    assert "external call change" not in risk.types


def test_substring_sql_in_identifier_does_not_trigger_persistence() -> None:
    cs = ChangeSummary(
        files=["app.py"],
        functions=[],
        diff_text=_diff(
            "app.py",
            added=[
                "mysql_url = 'redis://stub'",
                "PG_RESQL_NAME = 'demo'",
                "nosql_doc = {}",
            ],
        ),
    )
    risk = classify_risk(cs)
    assert "data persistence change" not in risk.types


def test_throw_word_in_docstring_does_not_trigger_error_handling() -> None:
    cs = ChangeSummary(
        files=["app.py"],
        functions=[],
        diff_text=_diff(
            "app.py",
            added=[
                '"""Docs: this function may throw a tantrum."""',
                "x = 1",
            ],
        ),
    )
    risk = classify_risk(cs)
    assert "error handling change" not in risk.types


# -- true positives must still fire ------------------------------------------


def test_real_persistence_change_python() -> None:
    cs = ChangeSummary(
        files=["repo.py"],
        functions=[],
        diff_text=_diff(
            "repo.py",
            added=[
                "    session.add(user)",
                "    session.commit()",
            ],
        ),
    )
    risk = classify_risk(cs)
    assert "data persistence change" in risk.types


def test_real_external_call_python() -> None:
    cs = ChangeSummary(
        files=["client.py"],
        functions=[],
        diff_text=_diff(
            "client.py",
            added=["    r = requests.get('https://api.example.com')"],
        ),
    )
    risk = classify_risk(cs)
    assert "external call change" in risk.types


def test_real_external_call_typescript() -> None:
    cs = ChangeSummary(
        files=["client.ts"],
        functions=[],
        diff_text=_diff(
            "client.ts",
            added=["  const r = await fetch('https://api.example.com')"],
        ),
    )
    risk = classify_risk(cs)
    assert "external call change" in risk.types


def test_real_error_handling_python() -> None:
    cs = ChangeSummary(
        files=["service.py"],
        functions=[],
        diff_text=_diff(
            "service.py",
            added=[
                "    try:",
                "        do_work()",
                "    except ValueError:",
                "        raise BadRequest('bad')",
            ],
        ),
    )
    risk = classify_risk(cs)
    assert "error handling change" in risk.types


def test_migration_file_marked_persistence() -> None:
    cs = ChangeSummary(
        files=["migrations/0001_init.sql"],
        functions=[],
        diff_text=_diff(
            "migrations/0001_init.sql",
            added=["CREATE TABLE users (id SERIAL PRIMARY KEY);"],
        ),
    )
    risk = classify_risk(cs)
    assert "data persistence change" in risk.types


def test_test_deletion_flagged() -> None:
    cs = ChangeSummary(
        files=["tests/test_foo.py"],
        functions=[],
        diff_text=_diff(
            "tests/test_foo.py",
            removed=[
                "def test_a(): assert True",
                "def test_b(): assert True",
                "def test_c(): assert True",
                "def test_d(): assert True",
            ],
        ),
    )
    risk = classify_risk(cs)
    assert "test coverage reduced" in risk.types


def test_auth_change_flagged() -> None:
    cs = ChangeSummary(
        files=["auth.py"],
        functions=[],
        diff_text=_diff(
            "auth.py",
            added=["    token = jwt.encode(payload, secret)"],
        ),
    )
    risk = classify_risk(cs)
    assert "auth/secrets change" in risk.types


def test_severity_low_for_tiny_unrelated_change() -> None:
    cs = ChangeSummary(
        files=["README.md"],
        functions=[],
        diff_text=_diff("README.md", added=["typo fix"]),
    )
    assert classify_risk(cs).level == "low"


def test_severity_high_when_persistence_and_many_files() -> None:
    cs = ChangeSummary(
        files=[f"models/m{i}.py" for i in range(5)],
        functions=[],
        diff_text=_diff("models/m0.py", added=["    session.commit()"]),
    )
    assert classify_risk(cs).level == "high"
