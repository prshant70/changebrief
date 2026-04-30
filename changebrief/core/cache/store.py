"""Branch/commit-aware cache for validation pipeline artifacts (7-day TTL).

Cache layout::

    ~/.changebrief/cache/<repo_id>/<base_sha>..<feature_sha>/<context_id>/<key>.json

``context_id`` includes the pipeline version, the model in use, and a hash of
the LLM contract (system prompt + JSON schema + prompt version). Bumping any
of these naturally invalidates older entries.

Writes are atomic (tempfile + ``os.replace``) so concurrent invocations cannot
produce a half-written JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional


DEFAULT_TTL_S = 7 * 24 * 60 * 60  # 7 days
PIPELINE_VERSION = 2


def _now() -> int:
    return int(time.time())


def get_repo_id(repo_path: Path) -> str:
    digest = hashlib.sha256(str(repo_path.resolve()).encode("utf-8")).hexdigest()
    return digest[:16]


def get_cache_root() -> Path:
    return Path.home() / ".changebrief" / "cache"


def build_context_id(*, model: str, prompt_hash: str) -> str:
    """Compose the per-pipeline cache namespace.

    Includes pipeline version, model identifier and a hash of the LLM contract
    (system prompt + JSON schema). Any change to those invalidates older
    entries automatically.
    """
    safe_model = (model or "unknown").replace("/", "_")
    return f"v{PIPELINE_VERSION}-{safe_model}-{prompt_hash}"


def get_cache_dir(
    *,
    repo_id: str,
    base_sha: str,
    feature_sha: str,
    context_id: str,
) -> Path:
    return get_cache_root() / repo_id / f"{base_sha}..{feature_sha}" / context_id


def _json_default(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically using a tempfile + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_cache(
    *,
    repo_id: str,
    base_sha: str,
    feature_sha: str,
    key: str,
    value: Any,
    context_id: str,
) -> Path:
    d = get_cache_dir(
        repo_id=repo_id,
        base_sha=base_sha,
        feature_sha=feature_sha,
        context_id=context_id,
    )
    payload = {
        "created_at": _now(),
        "ttl_s": DEFAULT_TTL_S,
        "pipeline_version": PIPELINE_VERSION,
        "context_id": context_id,
        "key": key,
        "value": value,
    }
    path = d / f"{key}.json"
    _atomic_write(path, json.dumps(payload, indent=2, default=_json_default) + "\n")
    return path


def read_cache(
    *,
    repo_id: str,
    base_sha: str,
    feature_sha: str,
    key: str,
    context_id: str,
    ttl_s: int = DEFAULT_TTL_S,
) -> Optional[Any]:
    path = (
        get_cache_dir(
            repo_id=repo_id,
            base_sha=base_sha,
            feature_sha=feature_sha,
            context_id=context_id,
        )
        / f"{key}.json"
    )
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    created_at = int(raw.get("created_at") or 0)
    if created_at <= 0:
        return None
    if _now() - created_at > int(ttl_s):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    if str(raw.get("context_id") or "") != context_id:
        # Defensive: stray file in this dir from a different context.
        return None
    return raw.get("value")


def is_expired(cache_file: Path, *, ttl_s: int = DEFAULT_TTL_S) -> bool:
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
        created_at = int(raw.get("created_at") or 0)
    except Exception:
        return True
    if created_at <= 0:
        return True
    return _now() - created_at > int(ttl_s)


def list_cache_items(*, cache_root: Optional[Path] = None) -> list[dict[str, str]]:
    """List cached entries as rows: ``{repo_id, pair, version, key, path}``."""
    root = cache_root or get_cache_root()
    if not root.is_dir():
        return []
    rows: list[dict[str, str]] = []
    for repo_dir in root.iterdir():
        if not repo_dir.is_dir():
            continue
        repo_id = repo_dir.name
        for pair_dir in repo_dir.iterdir():
            if not pair_dir.is_dir():
                continue
            pair = pair_dir.name
            for ctx_dir in pair_dir.iterdir():
                if not ctx_dir.is_dir():
                    continue
                version = ctx_dir.name
                for f in ctx_dir.glob("*.json"):
                    rows.append(
                        {
                            "repo_id": repo_id,
                            "pair": pair,
                            "version": version,
                            "key": f.stem,
                            "path": str(f),
                        },
                    )
    return rows


def purge_cache(
    *,
    repo_id: str | None = None,
    expired_only: bool = False,
    cache_root: Optional[Path] = None,
    ttl_s: int = DEFAULT_TTL_S,
) -> int:
    """Purge cache files. Returns number of files deleted."""
    root = cache_root or get_cache_root()
    if not root.is_dir():
        return 0
    deleted = 0

    def try_unlink(p: Path) -> None:
        nonlocal deleted
        try:
            p.unlink()
            deleted += 1
        except OSError:
            pass

    targets = [root / repo_id] if repo_id else [root]
    for t in targets:
        if not t.exists():
            continue
        for f in t.rglob("*.json"):
            if expired_only and not is_expired(f, ttl_s=ttl_s):
                continue
            try_unlink(f)

    # Best-effort remove empty directories (deep to shallow).
    for d in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if d.is_dir():
            try:
                next(d.iterdir())
            except StopIteration:
                try:
                    d.rmdir()
                except OSError:
                    pass
    return deleted
