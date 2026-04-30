#!/usr/bin/env bash
set -euo pipefail

# ChangeBrief installer
# Usage (recommended):
#   curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/<ref>/install.sh | bash
#
# Optional env overrides:
#   CHANGEBRIEF_REPO="owner/repo"          (default: prshant70/changebrief)
#   CHANGEBRIEF_REF="main"                (default: main)
#   CHANGEBRIEF_INSTALL_METHOD="pipx"     (pipx|pip) default: auto

_bold() { printf "\033[1m%s\033[0m\n" "$*"; }
_info() { printf "%s\n" "$*"; }
_warn() { printf "WARN: %s\n" "$*" >&2; }
_err() { printf "ERROR: %s\n" "$*" >&2; }

_die() { _err "$*"; exit 1; }

_need_cmd() {
  command -v "$1" >/dev/null 2>&1 || _die "Missing required command: $1"
}

_os="$(uname -s 2>/dev/null || true)"
case "$_os" in
  Darwin|Linux) ;;
  *) _die "Unsupported OS: ${_os}. Supported: macOS (Darwin) and Linux." ;;
esac

if [ "${EUID:-0}" -eq 0 ]; then
  _warn "Running as root is not recommended. Proceeding anyway."
fi

_need_cmd bash
_need_cmd curl
_need_cmd git

CHANGEBRIEF_REPO="${CHANGEBRIEF_REPO:-prshant70/changebrief}"
CHANGEBRIEF_REF="${CHANGEBRIEF_REF:-main}"
CHANGEBRIEF_INSTALL_METHOD="${CHANGEBRIEF_INSTALL_METHOD:-auto}"

_repo_url="https://github.com/${CHANGEBRIEF_REPO}.git"
_pkg_spec="git+${_repo_url}@${CHANGEBRIEF_REF}"

# Ensure temp builds happen in a writable directory (some environments restrict system temp).
_tmp_candidate="${HOME}/.changebrief/tmp"
if mkdir -p "${_tmp_candidate}" >/dev/null 2>&1; then
  export TMPDIR="${_tmp_candidate}"
else
  _tmp_candidate="/tmp/changebrief-${UID:-0}"
  mkdir -p "${_tmp_candidate}" >/dev/null 2>&1 || true
  export TMPDIR="${_tmp_candidate}"
fi

_pick_python() {
  local candidates=(python3.13 python3.12 python3.11 python3.10 python3)
  local c
  for c in "${candidates[@]}"; do
    if command -v "$c" >/dev/null 2>&1; then
      printf "%s" "$c"
      return 0
    fi
  done
  return 1
}

PYTHON="$(_pick_python || true)"
[ -n "${PYTHON}" ] || _die "Python 3.10+ is required but no python3 executable was found."

_py_ok="$("$PYTHON" - <<'PY'
import sys
ok = sys.version_info >= (3, 10)
print("ok" if ok else "no")
PY
)"

if [ "${_py_ok}" != "ok" ]; then
  _die "Python 3.10+ is required. Found: $("$PYTHON" -V 2>&1)"
fi

_bold "Installing ChangeBrief"
_info "- Repo: ${CHANGEBRIEF_REPO}"
_info "- Ref:  ${CHANGEBRIEF_REF}"
_info "- Python: $("$PYTHON" -V 2>&1)"
_info ""

_ensure_pip() {
  if "$PYTHON" -m pip --version >/dev/null 2>&1; then
    return 0
  fi
  _warn "pip not found for ${PYTHON}. Attempting to bootstrap ensurepip..."
  "$PYTHON" -m ensurepip --upgrade >/dev/null 2>&1 || true
  "$PYTHON" -m pip --version >/dev/null 2>&1 || _die "pip is required but could not be bootstrapped."
}

_install_with_pipx() {
  if ! command -v pipx >/dev/null 2>&1; then
    _info "pipx not found; installing pipx (user install)..."
    _ensure_pip
    "$PYTHON" -m pip install --user -U pipx >/dev/null
    # Best effort PATH guidance (pipx may not be on PATH in non-interactive shells)
    if ! command -v pipx >/dev/null 2>&1; then
      _warn "pipx installed but not found on PATH in this shell."
      _warn "You may need to add your Python user bin directory to PATH and re-run."
      _warn "Try: python -m pipx ensurepath"
      _die "pipx not available on PATH."
    fi
  fi

  _info "Installing via pipx (isolated environment)..."
  if pipx list 2>/dev/null | grep -qE '(^|\s)changebrief(\s|$)'; then
    pipx upgrade --spec "${_pkg_spec}" changebrief >/dev/null || pipx install --force "${_pkg_spec}" >/dev/null
  else
    pipx install "${_pkg_spec}" >/dev/null
  fi
}

_install_with_pip_user() {
  _ensure_pip
  _info "Installing via pip --user..."
  "$PYTHON" -m pip install --user -U "${_pkg_spec}" >/dev/null
}

case "${CHANGEBRIEF_INSTALL_METHOD}" in
  auto)
    if command -v pipx >/dev/null 2>&1; then
      _install_with_pipx
    else
      # Prefer pipx if possible, but avoid failing hard if PATH issues exist.
      if "$PYTHON" -m pip --version >/dev/null 2>&1; then
        _install_with_pip_user
      else
        _install_with_pipx
      fi
    fi
    ;;
  pipx) _install_with_pipx ;;
  pip) _install_with_pip_user ;;
  *) _die "Invalid CHANGEBRIEF_INSTALL_METHOD=${CHANGEBRIEF_INSTALL_METHOD}. Use: auto|pipx|pip" ;;
esac

_info ""
if command -v changebrief >/dev/null 2>&1; then
  _bold "✓ ChangeBrief installed"
  _info "Run: changebrief --help"
  _info "Version: $(changebrief --version 2>/dev/null || true)"
else
  _warn "Install completed, but 'changebrief' is not on PATH in this shell."
  _warn "If you used pip --user, ensure your user bin directory is on PATH."
  _warn "Try: python -m site --user-base (then add its 'bin' subdir to PATH)"
  _die "Could not verify installation on PATH."
fi

