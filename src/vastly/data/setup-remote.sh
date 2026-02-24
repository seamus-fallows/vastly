#!/usr/bin/env bash
set -euo pipefail

# ── Vastly remote setup script ─────────────────────────────────────────
# Standalone setup script for Vast.ai GPU instances.
# Receives config as positional arguments from the vastly CLI.
# Safe to re-run (idempotent).

# ── Arguments ───────────────────────────────────────────────────────────

REPO_URL="${1:?Usage: setup-remote.sh <repo_url> <repo_name> <git_name> <git_email> <workspace> <disable_auto_tmux> <install_command> <module_version> [post_install_commands...]}"
REPO_NAME="${2:?Missing repo_name}"
GIT_NAME="${3:?Missing git_name}"
GIT_EMAIL="${4:?Missing git_email}"
WORKSPACE="${5:?Missing workspace}"
DISABLE_AUTO_TMUX="${6:?Missing disable_auto_tmux}"
INSTALL_COMMAND="${7:?Missing install_command}"
MODULE_VERSION="${8:?Missing module_version}"
shift 8
POST_INSTALL_COMMANDS=("$@")

REPO_DIR="${WORKSPACE}/${REPO_NAME}"
BASHRC_MARKER="# vastly-managed"

# ── Environment ──────────────────────────────────────────────────────────

# Ensure ~/.local/bin is in PATH for the entire session (e.g., uv, claude)
export PATH="$HOME/.local/bin:$PATH"

# Suppress noisy pip warnings when running as root (expected on Vast.ai)
export PIP_ROOT_USER_ACTION=ignore
export PIP_PROGRESS_BAR=off

# Disable fancy terminal features -- progress bars / cursor movement garble
# output when piped through SSH to a Windows terminal
export TERM=dumb

# ── Helpers ─────────────────────────────────────────────────────────────

log() { echo ":: $*"; }
warn() { echo ":: [WARN] $*" >&2; }
die() { echo ":: [ERROR] $*" >&2; exit 1; }

# ── Step 1: Auto-tmux ──────────────────────────────────────────────────

if [[ "$DISABLE_AUTO_TMUX" == "true" ]]; then
    log "Disabling auto-tmux"
    touch ~/.no_auto_tmux
fi

# ── Step 2: Git identity ───────────────────────────────────────────────

log "Configuring git identity: ${GIT_NAME} <${GIT_EMAIL}>"
git config --global user.name "$GIT_NAME"
git config --global user.email "$GIT_EMAIL"

# ── Step 3: GitHub known hosts ─────────────────────────────────────────

if ! grep -q "^github.com " ~/.ssh/known_hosts 2>/dev/null; then
    log "Adding github.com to known_hosts"
    mkdir -p ~/.ssh
    ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null
fi

# ── Step 4: Clone ──────────────────────────────────────────────────────

mkdir -p "$WORKSPACE"
cd "$WORKSPACE"

if [[ -d "$REPO_NAME" ]]; then
    log "Repo already exists, skipping clone"
    cd "$REPO_NAME"
else
    log "Cloning ${REPO_URL} into ${REPO_DIR}"
    git clone "$REPO_URL" "$REPO_NAME" || die "git clone failed"
    cd "$REPO_NAME"
fi

# ── Step 5: Python environment detection ────────────────────────────────

# Find the best Python interpreter available on this instance.
setup_python() {
    # Prefer the venv python that many Vast images ship with
    if [[ -x /venv/main/bin/python ]]; then
        echo "/venv/main/bin/python"
        return
    fi

    # Try conda environments
    for conda_dir in /opt/conda /opt/miniforge3; do
        local conda_sh="${conda_dir}/etc/profile.d/conda.sh"
        if [[ -f "$conda_sh" ]]; then
            # shellcheck disable=SC1090
            source "$conda_sh" 2>/dev/null || true
            if conda activate main 2>/dev/null; then
                which python
                return
            fi
            if conda activate base 2>/dev/null; then
                which python
                return
            fi
        fi
    done

    # Fall back to system python
    if command -v python3 &>/dev/null; then
        which python3
    elif command -v python &>/dev/null; then
        which python
    else
        echo ""
    fi
}

PYTHON_PATH="$(setup_python)"

if [[ -z "$PYTHON_PATH" ]]; then
    warn "No Python interpreter found -- skipping dependency install"
fi

# ── Step 6: Install dependencies ────────────────────────────────────────

INSTALL_METHOD="none"

run_install() {
    local cmd="$1"
    log "Running: ${cmd}"
    eval "$cmd"
    INSTALL_METHOD="$cmd"
}

if [[ "$INSTALL_COMMAND" != "auto" ]]; then
    # Explicit install command from config
    run_install "$INSTALL_COMMAND"
elif [[ -n "$PYTHON_PATH" ]]; then
    # Auto-detect install method (checked in priority order)
    if [[ -f uv.lock ]] || ([[ -f pyproject.toml ]] && grep -q '\[tool\.uv\]' pyproject.toml 2>/dev/null); then
        # uv project -- ensure uv is available
        if ! command -v uv &>/dev/null; then
            log "Installing uv"
            curl -LsSf https://astral.sh/uv/install.sh | sh
        fi
        run_install "uv sync"
    elif [[ -f pyproject.toml ]] && grep -q '\[project\]' pyproject.toml 2>/dev/null; then
        run_install "\"${PYTHON_PATH}\" -m pip install -e ."
    elif compgen -G "requirements*.txt" >/dev/null 2>&1; then
        for reqfile in requirements*.txt; do
            run_install "\"${PYTHON_PATH}\" -m pip install -r ${reqfile}"
        done
    elif [[ -f setup.py ]]; then
        run_install "\"${PYTHON_PATH}\" -m pip install -e ."
    else
        log "No recognized Python project files found -- skipping dependency install"
        INSTALL_METHOD="none"
    fi
else
    log "No Python interpreter available -- skipping dependency install"
fi

# ── Step 7: Post-install commands ───────────────────────────────────────

if [[ ${#POST_INSTALL_COMMANDS[@]} -gt 0 ]]; then
    for cmd in "${POST_INSTALL_COMMANDS[@]}"; do
        [[ -z "$cmd" ]] && continue
        log "Post-install: ${cmd}"
        eval "$cmd" || warn "Post-install command failed: ${cmd}"
    done
fi

# ── Step 8: IDE settings ───────────────────────────────────────────────

# For VS Code settings, prefer the venv python, fall back to detected
VSCODE_PYTHON="$PYTHON_PATH"
if [[ -x /venv/main/bin/python ]]; then
    VSCODE_PYTHON="/venv/main/bin/python"
fi
[[ -z "$VSCODE_PYTHON" ]] && VSCODE_PYTHON="/usr/bin/python3"

log "Writing .vscode/settings.json (python: ${VSCODE_PYTHON})"
mkdir -p .vscode
cat > .vscode/settings.json << VSCEOF
{
    "python.defaultInterpreterPath": "${VSCODE_PYTHON}",
    "terminal.integrated.defaultProfile.linux": "bash (login)",
    "terminal.integrated.profiles.linux": {
        "bash (login)": {
            "path": "/bin/bash",
            "args": ["-l"]
        }
    }
}
VSCEOF

# ── Step 9: Patch .bashrc ──────────────────────────────────────────────

log "Patching .bashrc"

# Remove any previous vastly lines (idempotent)
if [[ -f ~/.bashrc ]]; then
    sed -i "/${BASHRC_MARKER}/d" ~/.bashrc
fi

# Build activation lines -- conda and venv can coexist
CONDA_LINE=""
VENV_LINE=""

# Always activate conda when available (so conda commands work)
for conda_dir in /opt/conda /opt/miniforge3; do
    if [[ -f "${conda_dir}/etc/profile.d/conda.sh" ]]; then
        CONDA_LINE="source ${conda_dir}/etc/profile.d/conda.sh && conda activate main 2>/dev/null || conda activate base 2>/dev/null ${BASHRC_MARKER}"
        break
    fi
done

# Also activate venv if it exists (venv PATH takes priority over conda)
if [[ -x /venv/main/bin/python ]]; then
    VENV_LINE="export VIRTUAL_ENV=/venv/main; export PATH=\"/venv/main/bin:\$PATH\" ${BASHRC_MARKER}"
fi

{
    echo "export PATH=\"\$HOME/.local/bin:\$PATH\" ${BASHRC_MARKER}"
    [[ -n "$CONDA_LINE" ]] && echo "$CONDA_LINE"
    [[ -n "$VENV_LINE" ]] && echo "$VENV_LINE"
    echo "cd ${REPO_DIR} ${BASHRC_MARKER}"
} >> ~/.bashrc

# ── Step 10: Ensure .bash_profile sources .bashrc ───────────────────────

# Remove old vastly lines from .bash_profile too, then re-add if needed
touch ~/.bash_profile
sed -i "/${BASHRC_MARKER}/d" ~/.bash_profile
if ! grep -q '\.bashrc' ~/.bash_profile 2>/dev/null; then
    log "Adding .bashrc source to .bash_profile"
    echo "[ -f ~/.bashrc ] && . ~/.bashrc ${BASHRC_MARKER}" >> ~/.bash_profile
fi

# ── Step 11: Write setup marker ───────────────────────────────────────

MARKER_DIR="$HOME/.vastly/setup"
TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

log "Writing setup marker to ${MARKER_DIR}/${REPO_NAME}.json"
mkdir -p "$MARKER_DIR"
cat > "${MARKER_DIR}/${REPO_NAME}.json" << MARKEREOF
{
    "timestamp": "${TIMESTAMP}",
    "installMethod": "${INSTALL_METHOD}",
    "moduleVersion": "${MODULE_VERSION}"
}
MARKEREOF

log "Setup complete for ${REPO_NAME}"
