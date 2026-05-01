from __future__ import annotations

from pathlib import Path

import pytest

from changebrief.core.ai_context import build_synthesizer as synth_mod
from changebrief.core.ai_context import dependency_learner as dep_mod


def test_strip_absolute_paths() -> None:
    assert synth_mod._strip_absolute_paths("see /opt/1mg/x") == "see `<path>`"
    assert synth_mod._strip_absolute_paths("see `/opt/1mg/x`") == "see `/opt/1mg/x`"
    assert synth_mod._strip_absolute_paths("") == ""


def test_cites_resolve_accepts_existing_file(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x=1", encoding="utf-8")
    assert synth_mod._cites_resolve(["a.py"], valid_paths=set(), repo_root=tmp_path) is True
    assert synth_mod._cites_resolve(["missing.py"], valid_paths=set(), repo_root=tmp_path) is False


def test_split_git_url_and_ref() -> None:
    url, ref = dep_mod._split_git_url_and_ref("git+ssh://git@bitbucket.org/acme/repo.git@v1.2.3")
    assert url and url.startswith("ssh://")
    assert ref == "v1.2.3"


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("v1.2.3", True),
        ("1.2.3", True),
        ("abcdef1", True),
        ("main", False),
        ("", False),
    ],
)
def test_is_pinned_ref(ref: str, expected: bool) -> None:
    assert dep_mod._is_pinned_ref(ref) is expected


def test_host_of_variants() -> None:
    assert dep_mod._host_of("file:///tmp/repo") == "file"
    assert dep_mod._host_of("https://github.com/a/b.git") == "github.com"
    assert dep_mod._host_of("ssh://git@bitbucket.org/acme/repo.git") == "bitbucket.org"
    assert dep_mod._host_of("git@bitbucket.org:acme/repo.git") == "bitbucket.org"


def test_drop_contradictions_prefers_instead() -> None:
    do = ["Use `Foo` instead of `Bar`."]
    dont = ["Don't use `Foo` here."]
    do2, dont2 = dep_mod._drop_contradictions(do, dont)
    # Prefer dropping the contradictory don't in this simple heuristic.
    assert "Foo" in "\n".join(do2)
    assert "Foo" not in "\n".join(dont2)


def test_evidence_only_filters() -> None:
    items = [
        "x",
        "y _(evidence: `a.py`)_",
        "**Public API** — `X`. Source: `pkg/__init__.py`.",
    ]
    kept = dep_mod._evidence_only(items)
    assert kept == ["y _(evidence: `a.py`)_", "**Public API** — `X`. Source: `pkg/__init__.py`."]


def test_discover_git_dependencies_pyproject_requirements_and_pipfile(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    # pyproject direct url
    (repo / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name="x"',
                "dependencies=[",
                '  "foo @ git+ssh://git@bitbucket.org/acme/foo.git@v1.2.3",',
                "]",
            ]
        ),
        encoding="utf-8",
    )
    # requirements.txt with egg fragment
    (repo / "requirements.txt").write_text(
        "git+https://github.com/acme/bar.git@abcdef1#egg=bar\n",
        encoding="utf-8",
    )
    # Pipfile
    (repo / "Pipfile").write_text(
        "\n".join(
            [
                "[[source]]",
                'url="https://pypi.org/simple"',
                'verify_ssl=true',
                'name="pypi"',
                "",
                "[packages]",
                'baz = {git = "https://github.com/acme/baz.git", ref = "v2.0.0"}',
            ]
        ),
        encoding="utf-8",
    )

    deps = dep_mod.discover_git_dependencies(repo)
    keyset = {(d.package_name, d.repo_url, d.ref) for d in deps}
    assert ("foo", "ssh://git@bitbucket.org/acme/foo.git", "v1.2.3") in keyset
    assert ("bar", "https://github.com/acme/bar.git", "abcdef1") in keyset
    assert ("baz", "https://github.com/acme/baz.git", "v2.0.0") in keyset


def test_discover_git_dependencies_node_package_json(tmp_path: Path) -> None:
    repo = tmp_path / "repo-node"
    repo.mkdir()
    (repo / "package.json").write_text(
        """
        {
          "name": "x",
          "dependencies": {
            "leftpad": "github:acme/leftpad#v1.0.0",
            "rightpad": "git+https://github.com/acme/rightpad.git@abcdef1"
          }
        }
        """,
        encoding="utf-8",
    )
    deps = dep_mod.discover_git_dependencies(repo)
    keyset = {(d.package_name, d.repo_url, d.ref) for d in deps}
    assert ("leftpad", "https://github.com/acme/leftpad.git", "v1.0.0") in keyset
    assert ("rightpad", "https://github.com/acme/rightpad.git", "abcdef1") in keyset


def test_requirements_file_supports_e_and_r(tmp_path: Path) -> None:
    repo = tmp_path / "repo-req"
    repo.mkdir()
    (repo / "more.txt").write_text(
        "-e git+https://github.com/acme/extra.git@v1.0.0#egg=extra\n",
        encoding="utf-8",
    )
    (repo / "requirements.txt").write_text(
        "\n".join(
            [
                "-r more.txt",
                "git+https://github.com/acme/base.git@abcdef1#egg=base",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    deps = dep_mod.discover_git_dependencies(repo)
    keyset = {(d.package_name, d.repo_url, d.ref) for d in deps}
    assert ("extra", "https://github.com/acme/extra.git", "v1.0.0") in keyset
    assert ("base", "https://github.com/acme/base.git", "abcdef1") in keyset


def test_pick_best_dep_prefers_highest_semver() -> None:
    deps = [
        dep_mod.GitDependency(package_name="x", repo_url="https://github.com/a/x.git", ref="v1.2.0"),
        dep_mod.GitDependency(package_name="x", repo_url="https://github.com/a/x.git", ref="v1.10.0"),
        dep_mod.GitDependency(package_name="x", repo_url="https://github.com/a/x.git", ref="v1.3.0"),
    ]
    picked = dep_mod._pick_best_dep(deps)
    assert picked.ref == "v1.10.0"


def test_cache_key_changes_with_llm_flag() -> None:
    k1 = dep_mod._cache_key("https://github.com/a/x.git", "v1.0.0", llm_enabled=True)
    k2 = dep_mod._cache_key("https://github.com/a/x.git", "v1.0.0", llm_enabled=False)
    assert k1 != k2


def test_ensure_named_framework_desc() -> None:
    assert dep_mod._ensure_named_framework_desc("sanic", "A web framework") == "`sanic` — A web framework"
    assert dep_mod._ensure_named_framework_desc("sanic", "`sanic` — already") == "`sanic` — already"

