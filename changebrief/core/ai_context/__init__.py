"""Generate per-agent context files (CLAUDE.md / CURSOR.md / CODEX.md).

Pipeline:
    scan_repo  →  load_context_config  →  compose_context  →  render
"""
