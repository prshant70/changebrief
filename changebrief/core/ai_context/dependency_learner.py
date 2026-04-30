"""Dependency learner for `ai-context init --enrich-deps`.

This module discovers pinned git dependencies (framework repos) in a consumer
repo, checks them out into a local cache, builds a framework context for each,
and returns a merged ContextConfig-like payload.

Design goals:
- Opt-in: this feature runs only when `--enrich-deps` is passed.
- Safe-ish defaults: only pinned refs (tag/sha) are processed; floating refs
  are skipped. The caller can restrict hosts.
- Cached: once a dependency ref has been processed, reuse the cached context.
- Deterministic-first: when the LLM is disabled, the framework context is still
  useful (public API surface, exceptions, pins, reference paths).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from changebrief.core.ai_context.builder import BuildReport, FrameworkEntry, build_framework_entry
from changebrief.core.ai_context.models import ContextConfig
from changebrief.core.ai_context.scanner import scan_repo
from changebrief.utils.paths import get_config_dir


@dataclass(frozen=True)
class GitDependency:
    """A pinned git dependency we can learn framework context from."""

    package_name: str  # import / package name in the consumer repo
    repo_url: str  # ssh/https/file url without the `git+` prefix when applicable
    ref: str  # tag or commit sha


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
_DEP_CACHE_VERSION = "deps-2"


def enrich_config_from_dependencies(
    repo_root: Path,
    *,
    config: dict,
    allow_hosts: Iterable[str],
    llm_enabled: bool,
) -> tuple[ContextConfig, List[GitDependency], List[str]]:
    """Return a ContextConfig fragment derived from pinned git dependencies.

    Returns:
    - ContextConfig: frameworks/do/dont/notes merged from learned deps
    - learned_deps: list of deps we successfully processed
    - skipped: human-readable reasons for skipped candidates
    """
    repo_root = repo_root.resolve()
    allow = {h.strip().lower() for h in allow_hosts if str(h).strip()}

    candidates = discover_git_dependencies(repo_root)
    learned: List[GitDependency] = []
    skipped: List[str] = []

    frameworks: Dict[str, str] = {}
    do: List[str] = []
    dont: List[str] = []
    notes: List[str] = []

    # If a dep is specified multiple times (pyproject + requirements + Pipfile, etc),
    # pick one deterministically per package name to avoid duplicate/conflicting
    # framework dumps in the consumer CLAUDE.md.
    by_name: Dict[str, List[GitDependency]] = {}
    for dep in candidates:
        by_name.setdefault(dep.package_name, []).append(dep)
    chosen: List[GitDependency] = []
    for name, deps in sorted(by_name.items(), key=lambda kv: kv[0]):
        if len(deps) == 1:
            chosen.append(deps[0])
            continue
        pick = _pick_best_dep(deps)
        chosen.append(pick)
        for other in deps:
            if other is pick:
                continue
            skipped.append(
                f"{name}: duplicate dependency spec ignored ({other.repo_url}@{other.ref})"
            )

    for dep in chosen:
        host = _host_of(dep.repo_url)
        if host and allow and host.lower() not in allow:
            skipped.append(f"{dep.package_name}: host {host!r} not in allowlist")
            continue
        if not dep.ref or not _is_pinned_ref(dep.ref):
            skipped.append(f"{dep.package_name}: unpinned ref (skipping)")
            continue

        try:
            entry = _load_or_build_entry(dep, config=config, llm_enabled=llm_enabled)
        except Exception as exc:  # defensive boundary: don't break init
            skipped.append(f"{dep.package_name}: failed to build context ({exc})")
            continue

        learned.append(dep)
        entry = _sanitize_entry_for_merge(entry)

        # Merge: never override an existing key for the same dep name; ordering is stable.
        if dep.package_name not in frameworks:
            frameworks[dep.package_name] = _ensure_named_framework_desc(dep.package_name, entry.description)
        for k, v in (entry.related_frameworks or {}).items():
            if k not in frameworks:
                frameworks[k] = _ensure_named_framework_desc(k, v)

        dep_do, dep_dont = _drop_contradictions(list(entry.do), list(entry.dont))

        # Enforce evidence gating and per-dependency caps. Keep only bullets that
        # include explicit evidence markers (LLM bullets now carry these; baseline
        # notes already include sources).
        for line in _cap(_evidence_only(dep_do), 3):
            if line and line not in do:
                do.append(line)
        for line in _cap(_evidence_only(dep_dont), 3):
            if line and line not in dont:
                dont.append(line)
        for line in _cap(_evidence_only(entry.notes), 4):
            if line and line not in notes:
                notes.append(line)

    return ContextConfig(frameworks=frameworks, do=do, dont=dont, notes=notes), learned, skipped


def discover_git_dependencies(repo_root: Path) -> List[GitDependency]:
    """Best-effort discovery of pinned git dependencies (Python + Node v1)."""
    out: List[GitDependency] = []
    out.extend(_discover_python_pyproject(repo_root))
    out.extend(_discover_python_requirements(repo_root))
    out.extend(_discover_python_pipfile(repo_root))
    out.extend(_discover_node_package_json(repo_root))
    # De-dupe by (name, url, ref)
    seen: set[tuple[str, str, str]] = set()
    deduped: List[GitDependency] = []
    for dep in out:
        key = (dep.package_name, dep.repo_url, dep.ref)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(dep)
    return deduped


def _pick_best_dep(deps: List[GitDependency]) -> GitDependency:
    """Pick the most specific/highest versioned dep among duplicates.

    Heuristics:
    - Prefer semver-like tags with the highest version (e.g. v1.2.3).
    - Else prefer full/partial SHA pins over non-sha refs.
    - Else fall back to a stable lexical ordering on (repo_url, ref).
    """

    def semver_tuple(ref: str) -> Optional[Tuple[int, int, int]]:
        m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", (ref or "").strip())
        if not m:
            return None
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # 1) highest semver
    semvered: List[Tuple[Tuple[int, int, int], GitDependency]] = []
    for d in deps:
        t = semver_tuple(d.ref)
        if t is not None:
            semvered.append((t, d))
    if semvered:
        semvered.sort(key=lambda kv: kv[0], reverse=True)
        return semvered[0][1]

    # 2) prefer sha-like refs
    sha = [d for d in deps if _SHA_RE.match((d.ref or "").strip() or "")]
    if sha:
        sha.sort(key=lambda d: (-len(d.ref.strip()), d.repo_url, d.ref))
        return sha[0]

    # 3) stable fallback
    return sorted(deps, key=lambda d: (d.repo_url, d.ref))[0]


def _sanitize_entry_for_merge(entry: FrameworkEntry) -> FrameworkEntry:
    # Ensure lists are unique and trimmed; keep order.
    def uniq(xs: List[str]) -> List[str]:
        out: List[str] = []
        for x in xs:
            s = str(x or "").strip()
            if not s:
                continue
            if s in out:
                continue
            out.append(s)
        return out

    return FrameworkEntry(
        name=entry.name,
        description=str(entry.description or "").strip(),
        related_frameworks={str(k).strip().lower(): str(v or "").strip() for k, v in (entry.related_frameworks or {}).items() if str(k or "").strip()},
        do=uniq(list(entry.do or [])),
        dont=uniq(list(entry.dont or [])),
        notes=uniq(list(entry.notes or [])),
    )


def _ensure_named_framework_desc(pkg: str, desc: str) -> str:
    """Avoid anonymous descriptions like 'A web framework...'."""
    pkg = (pkg or "").strip().lower()
    d = (desc or "").strip()
    if not pkg or not d:
        return d
    d_low = d.lower()
    if d_low.startswith(pkg) or f"`{pkg}`" in d:
        return d
    return f"`{pkg}` — {d}"


def _evidence_only(items: List[str]) -> List[str]:
    out: List[str] = []
    for raw in items:
        s = str(raw or "").strip()
        if not s:
            continue
        # Accept either the repo-enricher style evidence marker or deterministic "Source:" notes.
        if "_(evidence:" in s or "Source:" in s:
            out.append(s)
    return out


def _cap(items: List[str], n: int) -> List[str]:
    return items[: max(0, int(n))]


def _drop_contradictions(do: List[str], dont: List[str]) -> Tuple[List[str], List[str]]:
    """Remove obvious do/don't contradictions (best-effort heuristic)."""
    do_syms = _symbols_in(do)
    dont_syms = _symbols_in(dont)
    overlap = do_syms.intersection(dont_syms)
    if not overlap:
        return do, dont

    def keep_prefer_instead(line: str) -> bool:
        # If conflicting, prefer bullets that contain "instead" (usually more actionable).
        return "instead" in line.lower()

    def filter_lines(lines: List[str], *, drop_syms: set[str]) -> List[str]:
        out: List[str] = []
        for line in lines:
            syms = _symbols_in([line])
            if syms.intersection(drop_syms):
                out.append(line) if keep_prefer_instead(line) else None
            else:
                out.append(line)
        # Remove any Nones introduced above.
        return [x for x in out if x]

    # If both mention the same symbol, drop the weaker one(s).
    do2 = filter_lines(do, drop_syms=overlap)
    dont2 = filter_lines(dont, drop_syms=overlap)
    # If still contradictory, err on safety: drop the do-side mentions.
    if _symbols_in(do2).intersection(_symbols_in(dont2)):
        do2 = [ln for ln in do2 if not _symbols_in([ln]).intersection(overlap)]
    return do2, dont2


def _symbols_in(lines: List[str]) -> set[str]:
    syms: set[str] = set()
    for line in lines:
        for m in re.finditer(r"`([^`]{2,80})`", str(line or "")):
            token = m.group(1).strip()
            # Keep class/function-like tokens.
            if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", token):
                syms.add(token)
    return syms


# ---------------------------------------------------------------------------- python: pyproject.toml


_PYPROJECT_GIT_DEP_RE = re.compile(
    r"""
    (?P<name>[A-Za-z0-9_.-]+)          # distribution / package name
    \s*@\s*
    (?P<url>git\+[^\s'"]+)             # direct url (git+...)
    """,
    re.VERBOSE,
)


def _discover_python_pyproject(repo_root: Path) -> List[GitDependency]:
    pp = repo_root / "pyproject.toml"
    if not pp.is_file():
        return []
    try:
        text = pp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    out: List[GitDependency] = []
    for m in _PYPROJECT_GIT_DEP_RE.finditer(text):
        name = (m.group("name") or "").strip()
        raw = (m.group("url") or "").strip()
        repo_url, ref = _split_git_url_and_ref(raw)
        if not name or not repo_url or not ref:
            continue
        out.append(GitDependency(package_name=_normalise_pkg_name(name), repo_url=repo_url, ref=ref))
    return out


def _split_git_url_and_ref(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Parse `git+<url>@<ref>[#...]` with userinfo-safe ref detection."""
    s = raw
    if s.startswith("git+"):
        s = s[len("git+") :]
    # Drop fragments (subdirectory=, etc.)
    base, _, _frag = s.partition("#")
    # If there's no ref marker at all, skip.
    if "@" not in base:
        return base.strip() or None, None
    # Ref is typically the last '@' segment, but ssh urls include git@host.
    head, _, tail = base.rpartition("@")
    # Accept only tail that looks like a ref (no slashes, reasonably short).
    if not tail or "/" in tail or "\\" in tail:
        return base.strip() or None, None
    return head.strip() or None, tail.strip() or None


# ---------------------------------------------------------------------------- python: requirements.txt (pip)


def _discover_python_requirements(repo_root: Path) -> List[GitDependency]:
    """Parse requirements-style files for pinned git deps.

    Supports:
    - `name @ git+<url>@<ref>#...`
    - `-e git+<url>@<ref>#egg=name`
    - `git+<url>@<ref>#egg=name`
    - `-r other.txt` includes (one level deep, bounded)
    """
    for fname in ("requirements.txt", "requirements-dev.txt", "requirements.in"):
        path = repo_root / fname
        if not path.is_file():
            continue
        return _parse_requirements_file(path, repo_root, depth=0)
    return []


def _parse_requirements_file(path: Path, repo_root: Path, *, depth: int) -> List[GitDependency]:
    if depth > 2:
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    out: List[GitDependency] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            rel = line[3:].strip()
            inc = (path.parent / rel).resolve()
            try:
                inc.relative_to(repo_root.resolve())
            except ValueError:
                continue
            if inc.is_file():
                out.extend(_parse_requirements_file(inc, repo_root, depth=depth + 1))
            continue
        # Editable prefix.
        if line.startswith("-e "):
            line = line[3:].strip()
        # For git specs, keep `#egg=...` fragments; for everything else strip comments.
        if "git+" not in line:
            line = line.split("#", 1)[0].strip()
        spec = line.strip()
        # PEP 508 direct URL style.
        if " @ git+" in spec:
            name, _, url = spec.partition(" @ ")
            repo_url, ref = _split_git_url_and_ref(url.strip())
            if name and repo_url and ref:
                out.append(
                    GitDependency(
                        package_name=_normalise_pkg_name(name),
                        repo_url=repo_url,
                        ref=ref,
                    )
                )
            continue
        # Raw git+ url with egg (editable or not).
        if spec.startswith("git+"):
            repo_url, ref = _split_git_url_and_ref(spec)
            egg = _egg_name(spec)
            if egg and repo_url and ref:
                out.append(
                    GitDependency(
                        package_name=_normalise_pkg_name(egg),
                        repo_url=repo_url,
                        ref=ref,
                    )
                )
            continue
    return out


def _egg_name(spec: str) -> Optional[str]:
    # #egg=name or &egg=name
    if "#egg=" in spec:
        return spec.split("#egg=", 1)[1].split("&", 1)[0].strip() or None
    if "&egg=" in spec:
        return spec.split("&egg=", 1)[1].split("&", 1)[0].strip() or None
    return None


# ---------------------------------------------------------------------------- python: Pipfile (pipenv)


def _discover_python_pipfile(repo_root: Path) -> List[GitDependency]:
    """Parse Pipfile for pinned git deps (pipenv).

    Supported shapes:
    - [packages] name = {git = "url", ref = "sha"}   (or tag/rev)
    - [dev-packages] ...
    """
    pf = repo_root / "Pipfile"
    if not pf.is_file():
        return []
    try:
        import tomllib  # py3.11+
    except Exception:
        return []
    try:
        data = tomllib.loads(pf.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    out: List[GitDependency] = []
    for section in ("packages", "dev-packages"):
        pkgs = data.get(section) or {}
        if not isinstance(pkgs, dict):
            continue
        for name, val in pkgs.items():
            if not isinstance(val, dict):
                continue
            git_url = str(val.get("git") or "").strip()
            if not git_url:
                continue
            ref = str(val.get("ref") or val.get("tag") or val.get("rev") or "").strip()
            if not ref:
                continue
            # Pipfile stores plain url (no git+ prefix). Normalize to look like our repo urls.
            repo_url = git_url
            if repo_url.startswith("git+"):
                repo_url = repo_url[len("git+") :]
            out.append(GitDependency(package_name=_normalise_pkg_name(str(name)), repo_url=repo_url, ref=ref))
    return out


# ---------------------------------------------------------------------------- node: package.json


def _discover_node_package_json(repo_root: Path) -> List[GitDependency]:
    pkg = repo_root / "package.json"
    if not pkg.is_file():
        return []
    try:
        data = json.loads(pkg.read_text(encoding="utf-8", errors="replace") or "{}")
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    out: List[GitDependency] = []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        deps = data.get(section) or {}
        if not isinstance(deps, dict):
            continue
        for name, spec in deps.items():
            spec_s = str(spec or "").strip()
            if not spec_s:
                continue
            repo_url, ref = _parse_node_git_spec(spec_s)
            if not repo_url or not ref:
                continue
            out.append(GitDependency(package_name=_normalise_pkg_name(name), repo_url=repo_url, ref=ref))
    return out


def _parse_node_git_spec(spec: str) -> tuple[Optional[str], Optional[str]]:
    # github:org/repo#ref
    if spec.startswith("github:") and "#" in spec:
        rest = spec[len("github:") :]
        repo, _, ref = rest.partition("#")
        if repo and ref:
            return f"https://github.com/{repo}.git", ref
        return None, None
    # git+ssh://..., git+https://..., git+file://...
    if spec.startswith("git+"):
        return _split_git_url_and_ref(spec)
    return None, None


# ---------------------------------------------------------------------------- cache + build


def _load_or_build_entry(dep: GitDependency, *, config: dict, llm_enabled: bool) -> FrameworkEntry:
    key = _cache_key(dep.repo_url, dep.ref, llm_enabled=llm_enabled)
    cache_dir = get_config_dir() / "cache" / "ai-context-deps" / key
    ctx_path = cache_dir / "context.yaml"
    if ctx_path.is_file():
        return _entry_from_yaml(ctx_path.read_text(encoding="utf-8", errors="replace"), dep.package_name)

    repo_dir = cache_dir / "repo"
    _checkout_repo(dep.repo_url, dep.ref, dest=repo_dir)

    repo_ctx = scan_repo(repo_dir)
    report: BuildReport = build_framework_entry(
        repo_ctx,
        name_override=dep.package_name,
        config=config,
        llm_enabled=llm_enabled,
    )
    entry = report.entry

    cache_dir.mkdir(parents=True, exist_ok=True)
    ctx_path.write_text(_yaml_fragment(entry), encoding="utf-8")
    return entry


def _yaml_fragment(entry: FrameworkEntry) -> str:
    # Small, portable fragment. The consumer init merges this in-memory.
    payload: Dict[str, Any] = {
        "frameworks": {entry.name: entry.description, **(entry.related_frameworks or {})},
        "do": list(entry.do or []),
        "dont": list(entry.dont or []),
        "notes": list(entry.notes or []),
    }
    return yaml.safe_dump(payload, default_flow_style=False, sort_keys=True, allow_unicode=True)


def _entry_from_yaml(text: str, package_name: str) -> FrameworkEntry:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    frameworks = data.get("frameworks") or {}
    if not isinstance(frameworks, dict):
        frameworks = {}
    description = str(frameworks.get(package_name) or "").strip()
    related = {str(k): str(v) for k, v in frameworks.items() if str(k) != package_name}
    do = [str(x) for x in (data.get("do") or [])] if isinstance(data.get("do"), list) else []
    dont = [str(x) for x in (data.get("dont") or [])] if isinstance(data.get("dont"), list) else []
    notes = [str(x) for x in (data.get("notes") or [])] if isinstance(data.get("notes"), list) else []
    return FrameworkEntry(
        name=package_name,
        description=description,
        related_frameworks=related,
        do=do,
        dont=dont,
        notes=notes,
    )


def _checkout_repo(repo_url: str, ref: str, *, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Fast path for local repos: avoid creating a `.git/` (some sandboxed
    # environments block `.git/hooks` creation). Use `git archive` instead.
    local_src = _local_repo_path(repo_url)
    if local_src is not None and local_src.is_dir() and (local_src / ".git").exists():
        _checkout_via_archive(local_src, ref, dest=dest)
        return

    # Remote path: clone + checkout (best effort).
    if (dest / ".git").is_dir():
        _run(["git", "-C", str(dest), "fetch", "--tags", "--force", "--prune", "origin"])
        _run(["git", "-C", str(dest), "fetch", "--force", "--prune", "origin", ref])
        _run(["git", "-C", str(dest), "checkout", "--force", ref])
        return

    dest.mkdir(parents=True, exist_ok=True)
    # Prefer partial clone for speed, but some servers (incl. some Bitbucket
    # deployments) don't support it. Retry once without `--filter`.
    try:
        _run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                "--depth",
                "1",
                repo_url,
                str(dest),
            ]
        )
    except RuntimeError:
        _run(
            [
                "git",
                "clone",
                "--no-checkout",
                "--depth",
                "1",
                repo_url,
                str(dest),
            ]
        )
    _run(["git", "-C", str(dest), "fetch", "--tags", "--force", "--prune", "origin"])
    _run(["git", "-C", str(dest), "fetch", "--force", "--prune", "origin", ref])
    _run(["git", "-C", str(dest), "checkout", "--force", ref])


def _local_repo_path(repo_url: str) -> Optional[Path]:
    if repo_url.startswith("file://"):
        raw = repo_url[len("file://") :]
        return Path(raw)
    # Accept plain local paths as well.
    if "://" not in repo_url and (Path(repo_url).exists() or repo_url.startswith("/")):
        return Path(repo_url)
    return None


def _checkout_via_archive(src_repo: Path, ref: str, *, dest: Path) -> None:
    """Extract `src_repo@ref` into `dest` without creating a `.git/`."""
    marker = dest / ".changebrief_dep_ref"
    if marker.is_file():
        try:
            if marker.read_text(encoding="utf-8", errors="replace").strip() == ref.strip():
                return
        except OSError:
            pass
    if dest.exists():
        # Remove previous extraction.
        for child in dest.iterdir():
            if child.is_dir():
                import shutil

                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
    dest.mkdir(parents=True, exist_ok=True)

    # Pipe: git archive <ref> | tar -x -C dest
    proc_git = subprocess.Popen(
        ["git", "-C", str(src_repo), "archive", ref],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        proc_tar = subprocess.run(
            ["tar", "-x", "-C", str(dest)],
            stdin=proc_git.stdout,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    finally:
        if proc_git.stdout:
            proc_git.stdout.close()
    stderr_git = (proc_git.stderr.read().decode("utf-8", errors="replace") if proc_git.stderr else "")
    rc_git = proc_git.wait(timeout=90)
    if rc_git != 0:
        raise RuntimeError(f"git archive failed: {stderr_git.strip()}")
    if proc_tar.returncode != 0:
        raise RuntimeError(f"tar extract failed: {(proc_tar.stderr or '').strip()}")
    marker.write_text(ref.strip(), encoding="utf-8")


def _run(cmd: List[str]) -> None:
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        details = stderr or stdout or f"exit={proc.returncode}"
        raise RuntimeError(f"Command {cmd!r} failed: {details}")


def _cache_key(repo_url: str, ref: str, *, llm_enabled: bool) -> str:
    h = hashlib.sha256()
    h.update(f"v={_DEP_CACHE_VERSION}\n".encode("utf-8", errors="replace"))
    h.update(f"llm={'1' if llm_enabled else '0'}\n".encode("utf-8", errors="replace"))
    h.update(repo_url.strip().encode("utf-8", errors="replace"))
    h.update(b"\n")
    h.update(ref.strip().encode("utf-8", errors="replace"))
    return h.hexdigest()[:16]


def _normalise_pkg_name(name: str) -> str:
    return (name or "").strip().lower().replace("_", "-")


def _host_of(repo_url: str) -> Optional[str]:
    # file:// has no host; treat it as "file".
    if repo_url.startswith("file://"):
        return "file"
    # https://host/...
    m = re.match(r"^https?://([^/]+)/", repo_url)
    if m:
        return m.group(1)
    # ssh://git@host/...
    m = re.match(r"^ssh://[^@]+@([^/]+)/", repo_url)
    if m:
        return m.group(1)
    # scp-like: git@host:org/repo.git
    m = re.match(r"^[^@]+@([^:]+):", repo_url)
    if m:
        return m.group(1)
    return None


def _is_pinned_ref(ref: str) -> bool:
    # Accept tags like v1.2.3 and SHAs.
    r = (ref or "").strip()
    if not r:
        return False
    if _SHA_RE.match(r):
        return True
    # A conservative tag pattern. Reject obvious branch names like "main".
    if r.lower() in {"main", "master", "develop", "dev"}:
        return False
    if re.match(r"^[A-Za-z0-9][A-Za-z0-9._\-+/]{0,63}$", r):
        return True
    return False

