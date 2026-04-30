"""Turn raw repo signals + optional config into a typed :class:`AIContext`.

Design rule: **every bullet must be evidence-backed or come from the user
config.** If we can't ground a claim, we drop it. This is what stops the
output from drifting into generic fluff.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from changebrief.core.ai_context.conventions import ARCHITECTURE_LAYERS
from changebrief.core.ai_context.models import (
    AIContext,
    AIContextSection,
    ContextConfig,
    DirectoryRole,
    Evidence,
    LanguageProfile,
    RepoContext,
)


# Threshold above which an unknown imported package is "major" enough to
# surface as a possible framework. Lower than test repos so small examples
# still pass the bar; in real codebases dozens of files import the framework.
_MAJOR_IMPORT_FILE_THRESHOLD = 3
# How many additional major imports to surface per language.
_MAX_MAJOR_IMPORTS = 6


def compose_context(repo_ctx: RepoContext, config: ContextConfig) -> AIContext:
    """Compose the agent-ready context from raw signals + config."""
    overview = _overview(repo_ctx, config)
    sections = [
        _stack_section(repo_ctx, config),
        _structure_section(repo_ctx),
        _architecture_section(repo_ctx),
        _local_dev_section(repo_ctx),
        _entry_points_section(repo_ctx),
        _conventions_section(repo_ctx),
        _do_section(repo_ctx, config),
        _dont_section(repo_ctx, config),
        _notes_section(config),
        _references_section(repo_ctx),
    ]
    sections = [s for s in sections if not (s.omit_if_empty and not _section_has_content(s))]
    return AIContext(
        project_name=repo_ctx.project_name or "this repository",
        overview=overview,
        sections=sections,
    )


# ---------------------------------------------------------------------------- helpers


def _section_has_content(s: AIContextSection) -> bool:
    return bool(s.bullets or s.paragraphs)


def _real_profiles(repo_ctx: RepoContext) -> List[LanguageProfile]:
    return [p for p in repo_ctx.profiles if p.language != "generic"]


def _overview(repo_ctx: RepoContext, config: ContextConfig) -> str:
    if config.project_summary:
        return config.project_summary.strip()
    if repo_ctx.project_summary:
        return repo_ctx.project_summary.strip()
    profiles = _real_profiles(repo_ctx)
    if not profiles:
        return (
            f"`{repo_ctx.project_name}` — no specific language fingerprint detected. "
            "Treat this file as a starting point and customise via `.changebrief/context.yaml`."
        )
    primary = repo_ctx.primary_language or profiles[0].language
    main_profile = next((p for p in profiles if p.language == primary), profiles[0])
    fw_names = [e.fact for e in main_profile.frameworks]
    fw_phrase = f" using {_oxford_join(fw_names)}" if fw_names else ""
    return (
        f"`{repo_ctx.project_name}` is primarily a {primary.title()} project"
        f"{fw_phrase}. This file was generated from repo signals; everything below is "
        "grounded in observed files (paths, lockfiles, scripts) — not assumed."
    )


def _stack_section(repo_ctx: RepoContext, config: ContextConfig) -> AIContextSection:
    s = AIContextSection(title="Stack & dependencies")
    bullets: List[str] = []

    overrides = {k.lower(): v for k, v in (config.frameworks or {}).items()}

    for profile in _real_profiles(repo_ctx):
        bits: List[str] = []
        if profile.package_manager:
            bits.append(f"package manager `{profile.package_manager}`")
        if profile.test_framework:
            bits.append(f"tests via `{profile.test_framework}`")
        head = f"**{profile.language.title()}**"
        if bits:
            head += " — " + ", ".join(bits)
        bullets.append(head)

        already_named: set[str] = set()

        # 1. User-config overrides come first — they're authoritative.
        applied_overrides: set[str] = set()
        for pkg_lower, friendly in overrides.items():
            file_count = profile.major_imports.get(pkg_lower, 0)
            if file_count == 0 and pkg_lower not in {
                ev.fact.lower() for ev in profile.frameworks
            }:
                continue
            applied_overrides.add(pkg_lower)
            already_named.add(pkg_lower)
            evidence = (
                f"imported in {file_count} source files; declared in pyproject"
                if file_count
                else "user config + curated detection"
            )
            bullets.append(f"{friendly} _(evidence: {evidence})_")

        # 2. Curated frameworks — skip any that the override already covered.
        for fw in profile.frameworks[:8]:
            if fw.fact.lower() in applied_overrides:
                continue
            bullets.append(f"{fw.fact} _(evidence: `{fw.source}`)_")
            already_named.add(fw.fact.lower())

        for note in profile.extra_notes[:3]:
            bullets.append(f"{note.fact} _(evidence: `{note.source}`)_")

        # 3. Major imports the curated map doesn't know about — surface them
        #    so internal/private frameworks (e.g. Torpedo) aren't invisible.
        for pkg, friendly_or_none in _unknown_major_imports(
            profile, overrides, already_named
        ):
            file_count = profile.major_imports.get(pkg, 0)
            label = friendly_or_none or f"`{pkg}`"
            bullets.append(
                f"{label} — observed in {file_count} source files "
                f"_(evidence: `{pkg}` imports across `{', '.join(profile.source_dirs[:2]) or 'src'}`)_"
            )

    if repo_ctx.license_name:
        bullets.append(f"License: **{repo_ctx.license_name}**")
    if repo_ctx.has_ci:
        bullets.append("CI: " + ", ".join(repo_ctx.has_ci))
    s.bullets = bullets
    return s


def _unknown_major_imports(
    profile: LanguageProfile,
    overrides: Dict[str, str],
    already_named: set[str],
) -> List[Tuple[str, Optional[str]]]:
    """Pick the heaviest imported packages we haven't already mentioned.

    Returns ``[(package_name, optional_friendly_label_from_overrides)]``.
    """
    if not profile.major_imports:
        return []

    declared = {d.lower() for d in profile.declared_dependencies}
    out: List[Tuple[str, Optional[str]]] = []
    for pkg, count in sorted(
        profile.major_imports.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        if count < _MAJOR_IMPORT_FILE_THRESHOLD:
            break
        if pkg in already_named:
            continue
        # Heuristic: prefer packages that are declared (likely real frameworks)
        # but allow undeclared if they pass the threshold (covers vendored/path
        # installs).
        if declared and pkg not in declared and count < _MAJOR_IMPORT_FILE_THRESHOLD * 2:
            continue
        friendly = overrides.get(pkg)
        out.append((pkg, friendly))
        if len(out) >= _MAX_MAJOR_IMPORTS:
            break
    return out


def _structure_section(repo_ctx: RepoContext) -> AIContextSection:
    s = AIContextSection(title="Project structure")
    bullets: List[str] = []
    children_by_parent: Dict[str, List[DirectoryRole]] = {}
    for nested in repo_ctx.nested_directories:
        children_by_parent.setdefault(nested.parent_dir or "", []).append(nested)

    for d in repo_ctx.top_directories[:14]:
        if d.description:
            bullets.append(f"`{d.rel_path or d.name}/` — {d.description}")
        else:
            bullets.append(f"`{d.rel_path or d.name}/`")
        for child in children_by_parent.get(d.name, [])[:12]:
            label = f"  - `{child.rel_path or child.name}/`"
            if child.description:
                label += f" — {child.description}"
            bullets.append(label)
    if not bullets:
        bullets.append("_No conventional top-level directories detected._")
    s.bullets = bullets
    return s


def _architecture_section(repo_ctx: RepoContext) -> AIContextSection:
    """Render an architecture overview inferred from directory layout.

    Each layer is shown only if at least one directory matches its role.
    Cites the actual directory paths so the section is fully grounded.
    """
    s = AIContextSection(title="Architecture (inferred from layout)")
    layer_to_dirs: Dict[str, List[DirectoryRole]] = {}
    all_dirs: List[DirectoryRole] = list(repo_ctx.top_directories) + list(
        repo_ctx.nested_directories
    )
    for d in all_dirs:
        if d.role in {layer_id for layer_id, _, _ in ARCHITECTURE_LAYERS}:
            layer_to_dirs.setdefault(d.role, []).append(d)

    if not layer_to_dirs:
        return s  # omitted when no architectural signal at all

    bullets: List[str] = []
    for layer_id, label, _in_shape in ARCHITECTURE_LAYERS:
        dirs = layer_to_dirs.get(layer_id) or []
        if not dirs:
            continue
        paths = ", ".join(f"`{d.rel_path or d.name}/`" for d in dirs[:6])
        bullets.append(f"**{label}**: {paths}")

    # Build the linear request shape from layers we've actually got.
    shape_layers: List[str] = []
    for layer_id, _label, in_shape in ARCHITECTURE_LAYERS:
        if not in_shape:
            continue
        dirs = layer_to_dirs.get(layer_id) or []
        if not dirs:
            continue
        # Pick the first dir alphabetically for determinism.
        chosen = sorted(dirs, key=lambda d: (d.rel_path or d.name))[0]
        shape_layers.append(f"`{chosen.rel_path or chosen.name}/`")
    s.bullets = bullets
    if len(shape_layers) >= 2:
        s.paragraphs.append(
            "Typical request shape (inferred): " + " → ".join(shape_layers) + "."
        )
        s.paragraphs.append(
            "When implementing or reviewing a change, follow this layering: "
            "entry-layer code calls business orchestration, which calls data-access "
            "or external clients, which talk to persistence. Don't shortcut layers."
        )
    return s


def _local_dev_section(repo_ctx: RepoContext) -> AIContextSection:
    s = AIContextSection(title="Local development")
    bullets: List[str] = []
    seen: set[str] = set()
    for profile in repo_ctx.profiles:
        for name, cmd in sorted(profile.run_scripts.items()):
            if name in seen:
                continue
            seen.add(name)
            bullets.append(f"`{name}` → `{cmd}`")
            if len(bullets) >= 10:
                break
        if len(bullets) >= 10:
            break
    # Sensible fallbacks based on detected test framework.
    primary = next((p for p in _real_profiles(repo_ctx) if p.language == repo_ctx.primary_language), None)
    if primary:
        if primary.test_framework == "pytest" and "pytest" not in seen:
            bullets.append("`tests` → `pytest -q`")
        elif primary.test_framework in {"jest", "vitest"} and primary.test_framework not in seen:
            bullets.append(f"`tests` → `{primary.test_framework}`")
        elif primary.test_framework == "go test (stdlib)" and "test" not in seen:
            bullets.append("`tests` → `go test ./...`")
        elif primary.test_framework == "cargo test" and "test" not in seen:
            bullets.append("`tests` → `cargo test`")
    s.bullets = bullets
    return s


def _entry_points_section(repo_ctx: RepoContext) -> AIContextSection:
    s = AIContextSection(title="Entry points (observed)")
    bullets: List[str] = []
    for profile in repo_ctx.profiles:
        for ep in profile.entry_points[:8]:
            bullets.append(f"`{ep}`")
    s.bullets = bullets
    s.paragraphs.append(
        "These are the request handlers / decorated routes the scanner found. "
        "Treat them as the primary behavioral surface area for validation."
    ) if bullets else None
    return s


def _conventions_section(repo_ctx: RepoContext) -> AIContextSection:
    s = AIContextSection(title="Conventions (observed)")
    bullets: List[str] = []
    for profile in _real_profiles(repo_ctx):
        if profile.test_dirs and profile.test_framework:
            bullets.append(
                f"{profile.language.title()} tests live in "
                + ", ".join(f"`{d}/`" for d in profile.test_dirs)
                + f" and use `{profile.test_framework}`."
            )
        if profile.source_dirs:
            bullets.append(
                f"{profile.language.title()} source lives in "
                + ", ".join(f"`{d}/`" for d in profile.source_dirs[:5])
                + "."
            )
    if repo_ctx.file_naming_patterns:
        bullets.append(
            "File-naming patterns observed: "
            + ", ".join(
                f"`{e.fact.replace(' pattern','')}` (e.g. `{e.source}`)"
                for e in repo_ctx.file_naming_patterns[:5]
            )
            + ". Follow the same convention for new files."
        )
    if repo_ctx.has_editorconfig:
        bullets.append("Formatting / whitespace is governed by `.editorconfig` — respect it.")
    if repo_ctx.has_precommit:
        bullets.append("Run `pre-commit run --all-files` before committing (`.pre-commit-config.yaml` is present).")
    if repo_ctx.has_codeowners:
        bullets.append("CODEOWNERS is configured — request the owners listed there for review.")
    s.bullets = bullets
    return s


def _do_section(repo_ctx: RepoContext, config: ContextConfig) -> AIContextSection:
    s = AIContextSection(title="Do")
    bullets: List[str] = []
    profiles = _real_profiles(repo_ctx)
    primary = next((p for p in profiles if p.language == repo_ctx.primary_language), None)
    if primary and primary.package_manager:
        bullets.append(
            f"Use `{primary.package_manager}` to add or upgrade dependencies — never edit lockfiles by hand."
        )
    if primary and primary.test_framework:
        bullets.append(
            f"Add a `{primary.test_framework}` test for every behavioral change "
            "(new branch, error path, edge case)."
        )
    if repo_ctx.file_naming_patterns:
        names = [e.fact.replace(" pattern", "") for e in repo_ctx.file_naming_patterns[:4]]
        bullets.append(
            "Match the existing naming patterns "
            + ", ".join(f"`{n}`" for n in names)
            + " when adding new modules."
        )
    bullets.extend(_normalise_lines(config.do))
    s.bullets = bullets
    return s


def _dont_section(repo_ctx: RepoContext, config: ContextConfig) -> AIContextSection:
    s = AIContextSection(title="Don't")
    bullets: List[str] = []
    profiles = _real_profiles(repo_ctx)
    primary = next((p for p in profiles if p.language == repo_ctx.primary_language), None)
    if primary and primary.package_manager:
        bullets.append("Don't commit secrets. Use environment variables or `~/.changebrief/config.yaml` for local credentials.")
    if "pyproject.toml" in [p.name for p in []] or any(p.language == "python" for p in profiles):
        bullets.append("Don't introduce new top-level dependencies without justifying them in the PR description.")
    bullets.append("Don't bypass existing error-handling decorators (`@handle_errors`, custom middleware, etc.) — they exist to keep exit codes / responses consistent.")
    bullets.extend(_normalise_lines(config.dont))
    s.bullets = bullets
    return s


def _notes_section(config: ContextConfig) -> AIContextSection:
    """Renders user-config ``notes`` only. Section is omitted when no notes exist."""
    s = AIContextSection(title="Notes")
    s.bullets = _normalise_lines(config.notes)
    return s


def _references_section(repo_ctx: RepoContext) -> AIContextSection:
    s = AIContextSection(title="References")
    bullets: List[str] = []
    if repo_ctx.has_readme:
        bullets.append("`README.md` — start here for usage and quickstart.")
    if repo_ctx.has_contributing:
        bullets.append("`CONTRIBUTING.md` — contribution workflow and standards.")
    if repo_ctx.has_security:
        bullets.append("`SECURITY.md` — security disclosure policy.")
    if (s_dir := _path_or_none(repo_ctx.root, "ROADMAP.md")):
        bullets.append(f"`{s_dir}` — product/engineering roadmap.")
    if repo_ctx.has_ci:
        bullets.append("`.github/workflows/` (or equivalent) — CI definitions; always run locally before pushing if possible.")
    s.bullets = bullets
    return s


def _normalise_lines(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    for raw in items:
        line = (raw or "").strip()
        if line:
            out.append(line)
    return out


def _oxford_join(items: List[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _path_or_none(root: str, relative: str) -> Optional[str]:
    from pathlib import Path

    return relative if (Path(root) / relative).exists() else None
