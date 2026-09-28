"""Load and manage ~/.vastly/config.json configuration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from importlib import resources
from pathlib import Path
from typing import Any, TypedDict

import vastly
from vastly import yellow
from vastly.errors import ConfigError


class PortForward(TypedDict):
    local: int
    remote: int


class Config(TypedDict):
    ide: str
    sshKeyPath: str | None
    sshUser: str
    portForwards: list[PortForward]
    workspace: str
    disableAutoTmux: bool
    gitRemote: str
    postInstall: list[str]
    installCommand: str | None
    copyFiles: list[str]
    gitAuth: str


CONFIG_DIR = Path.home() / ".vastly"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULTS = {
    "ide": "code",
    "sshKeyPath": None,
    "sshUser": "root",
    "portForwards": [{"local": 8080, "remote": 8080}],
    "workspace": "/workspace",
    "disableAutoTmux": False,
    "gitRemote": "origin",
    "postInstall": [],
    "installCommand": None,
    "copyFiles": [],
    "gitAuth": "agent",
}

# How instances get git access: forward your SSH agent, use a per-repo GitHub
# deploy key where possible, or require a deploy key (see gitauth.py)
GIT_AUTH_MODES = ("agent", "auto", "deploy-key")

_STRING_KEYS = frozenset({"ide", "sshUser", "workspace", "gitRemote", "gitAuth"})


def _ensure_list(config: dict, key: str) -> None:
    """Coerce a single string value to a one-element list."""
    if isinstance(config.get(key), str):
        config[key] = [config[key]]


_PROJECT_KEYS = frozenset(
    {
        "postInstall",
        "installCommand",
        "workspace",
        "portForwards",
        "copyFiles",
        "gitRemote",
    }
)


def _ide_from_env() -> str | None:
    """Detect the current IDE from environment variables.

    Returns "code", "cursor", or None if not inside an IDE terminal.
    """
    # Cursor sets CURSOR_TRACE_ID in all its terminals.  Check this first
    # because Cursor also sets TERM_PROGRAM=vscode (it's a VS Code fork).
    if os.environ.get("CURSOR_TRACE_ID"):
        return "cursor"

    term = os.environ.get("TERM_PROGRAM", "").lower()
    if "vscode" in term:
        return "code"

    # Windows fallback: check VSCODE_* paths for "cursor" vs "Code".
    for key in ("VSCODE_GIT_ASKPASS_MAIN", "VSCODE_CODE_CACHE_PATH"):
        val = os.environ.get(key, "")
        if not val:
            continue
        if "cursor" in val.lower():
            return "cursor"
        return "code"

    return None


def _detect_ide() -> str:
    """Detect which IDE the user likely wants.

    Checks terminal environment first, then falls back to what's installed.
    """
    from_env = _ide_from_env()
    if from_env:
        return from_env

    # Not inside an IDE terminal -- check what's installed
    has_code = shutil.which("code") is not None
    has_cursor = shutil.which("cursor") is not None
    if has_cursor and not has_code:
        return "cursor"
    return "code"


_KNOWN_KEYS = frozenset(DEFAULTS)


def _validate_config(config: Config) -> None:
    """Validate types and basic shapes of a resolved config dict.

    Raises ConfigError with a clear message on the first problem found.
    """
    # --- non-empty string keys ---
    for key in ("ide", "sshUser", "workspace", "gitRemote", "gitAuth"):
        val = config.get(key)
        if not isinstance(val, str) or not val:
            raise ConfigError(
                f"Invalid config: '{key}' must be a non-empty string, "
                f"got {type(val).__name__}"
            )

    if config["gitAuth"] not in GIT_AUTH_MODES:
        raise ConfigError(
            f"Invalid config: 'gitAuth' must be one of {', '.join(GIT_AUTH_MODES)}, "
            f"got {config['gitAuth']!r}"
        )

    # workspace must start with /
    if not config["workspace"].startswith("/"):
        raise ConfigError(
            f"Invalid config: 'workspace' must start with '/', "
            f"got {config['workspace']!r}"
        )

    # sshKeyPath: None or non-empty string
    ssh_key_path = config["sshKeyPath"]
    if ssh_key_path is not None and (
        not isinstance(ssh_key_path, str) or not ssh_key_path
    ):
        raise ConfigError(
            f"Invalid config: 'sshKeyPath' must be None or a non-empty string, "
            f"got {type(ssh_key_path).__name__}"
        )

    # disableAutoTmux: bool
    disable_tmux = config["disableAutoTmux"]
    if not isinstance(disable_tmux, bool):
        raise ConfigError(
            f"Invalid config: 'disableAutoTmux' must be a bool, "
            f"got {type(disable_tmux).__name__}"
        )

    # installCommand: None or non-empty string
    install_cmd = config["installCommand"]
    if install_cmd is not None and (
        not isinstance(install_cmd, str) or not install_cmd
    ):
        raise ConfigError(
            f"Invalid config: 'installCommand' must be None or a non-empty string, "
            f"got {type(install_cmd).__name__}"
        )

    # portForwards: list of dicts with int local and int remote
    port_forwards = config["portForwards"]
    if not isinstance(port_forwards, list):
        raise ConfigError(
            f"Invalid config: 'portForwards' must be a list of {{local, remote}} objects, "
            f"got {type(port_forwards).__name__}"
        )
    for i, entry in enumerate(port_forwards):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"Invalid config: 'portForwards[{i}]' must be a {{local, remote}} object, "
                f"got {type(entry).__name__}"
            )
        for field in ("local", "remote"):
            if field not in entry or not isinstance(entry[field], int):
                actual = (
                    type(entry.get(field)).__name__ if field in entry else "missing"
                )
                raise ConfigError(
                    f"Invalid config: 'portForwards[{i}].{field}' must be an int, "
                    f"got {actual}"
                )
            if not (1 <= entry[field] <= 65535):
                raise ConfigError(
                    f"Invalid config: 'portForwards[{i}].{field}' must be 1-65535, "
                    f"got {entry[field]}"
                )

    # postInstall, copyFiles: list of strings
    for key in ("postInstall", "copyFiles"):
        val = config[key]
        if not isinstance(val, list):
            raise ConfigError(
                f"Invalid config: '{key}' must be a list of strings, "
                f"got {type(val).__name__}"
            )
        for i, entry in enumerate(val):
            if not isinstance(entry, str):
                raise ConfigError(
                    f"Invalid config: '{key}[{i}]' must be a string, "
                    f"got {type(entry).__name__}"
                )


def _warn_unknown_keys(raw: dict[str, Any], source: str) -> None:
    """Print a warning to stderr for any unrecognized top-level keys."""
    unknown = set(raw) - _KNOWN_KEYS
    if unknown:
        keys = ", ".join(sorted(unknown))
        print(
            yellow(f"Warning: unknown config keys in {source}: {keys}"), file=sys.stderr
        )


def ensure_config(path: Path | None = None) -> bool:
    """Create config from template if missing. Returns True if created."""
    path = path or CONFIG_PATH
    if path.exists():
        return False
    template = resources.files("vastly.data").joinpath(".vastly.template.json")
    config_data = json.loads(template.read_text(encoding="utf-8"))
    config_data["ide"] = _detect_ide()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config_data, indent=2) + "\n", encoding="utf-8")
    return True


def load_config(path: Path | None = None, *, project_dir: Path | None = None) -> Config:
    """Load config from disk.

    If ``project_dir`` is given and contains a ``.vastly.json``, project-specific
    keys (postInstall, installCommand, workspace, portForwards, copyFiles,
    gitRemote) are overlaid on the global config.  User-specific keys (ide,
    sshKeyPath, sshUser, disableAutoTmux, gitAuth) in a project config are
    silently ignored -- a repo must not be able to turn on agent forwarding.
    """
    path = path or CONFIG_PATH

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"Invalid JSON in {path}: {e}\n"
            "Fix the file or delete it to regenerate from template."
        ) from e
    except OSError as e:
        raise ConfigError(f"Can't read {path}: {e.strerror or e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"Invalid config in {path}: expected a JSON object {{...}}")

    _warn_unknown_keys(raw, str(path))

    config = {}
    for k, v in DEFAULTS.items():
        user_val = raw.get(k)
        if user_val is None or (k in _STRING_KEYS and user_val == ""):
            config[k] = v
        elif k == "sshKeyPath" and user_val == "":
            config[k] = None
        else:
            config[k] = user_val

    # Auto-detect IDE from terminal environment (overrides config when inside an IDE).
    env_ide = _ide_from_env()
    if env_ide:
        vastly.verbose(f"IDE detected from environment: {env_ide}")
        config["ide"] = env_ide

    _ensure_list(config, "postInstall")
    _ensure_list(config, "copyFiles")

    # Overlay per-project config (only project-specific keys)
    # TODO: consider merging global + project postInstall instead of replacing,
    # so users can have global commands (e.g. install claude) plus project-specific ones.
    if project_dir:
        project_cfg = project_dir / ".vastly.json"
        if project_cfg.exists():
            try:
                project_raw = json.loads(project_cfg.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise ConfigError(
                    f"Invalid JSON in {project_cfg}: {e}\n"
                    "Fix the project config or remove it."
                ) from e
            except OSError as e:
                raise ConfigError(f"Can't read {project_cfg}: {e.strerror or e}") from e
            if not isinstance(project_raw, dict):
                raise ConfigError(
                    f"Invalid config in {project_cfg}: expected a JSON object {{...}}"
                )

            _warn_unknown_keys(project_raw, str(project_cfg))

            for k, v in project_raw.items():
                if k not in _PROJECT_KEYS:
                    continue
                if v is None or (k in _STRING_KEYS and v == ""):
                    continue
                config[k] = v

            _ensure_list(config, "postInstall")
            _ensure_list(config, "copyFiles")

            vastly.verbose(f"Project config overlaid from {project_cfg}")

    _validate_config(config)
    vastly.verbose(f"Config loaded from {path}")
    return config


# ── Project command approval ────────────────────────────────────────
#
# A repo's .vastly.json can run shell commands on your instance (installCommand,
# postInstall). vastly asks before running them the first time, and again
# whenever they change. Approvals are kept per repo URL, as a hash of the
# commands, in ~/.vastly/approved-commands.json.


def project_commands(project_dir: Path | None) -> dict[str, Any]:
    """Shell commands a repo's .vastly.json would run on instances ({} if none)."""
    if not project_dir:
        return {}
    project_cfg = project_dir / ".vastly.json"
    try:
        raw = json.loads(project_cfg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}  # missing, or already reported by load_config()
    if not isinstance(raw, dict):
        return {}

    commands: dict[str, Any] = {}
    install = raw.get("installCommand")
    if isinstance(install, str) and install:
        commands["installCommand"] = install
    post = raw.get("postInstall")
    post = [post] if isinstance(post, str) else post
    if isinstance(post, list):
        post = [c for c in post if isinstance(c, str) and c]
        if post:
            commands["postInstall"] = post
    return commands


def _approvals_file() -> Path:
    return CONFIG_DIR / "approved-commands.json"


def _load_approvals() -> dict[str, str]:
    try:
        approvals = json.loads(_approvals_file().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return approvals if isinstance(approvals, dict) else {}


def _commands_hash(commands: dict[str, Any]) -> str:
    encoded = json.dumps(commands, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def commands_approved(repo_url: str, commands: dict[str, Any]) -> bool:
    """Whether these exact project commands were approved for this repo."""
    return _load_approvals().get(repo_url) == _commands_hash(commands)


def approve_commands(repo_url: str, commands: dict[str, Any]) -> None:
    """Remember that the user approved these project commands for this repo."""
    approvals = _load_approvals()
    approvals[repo_url] = _commands_hash(commands)
    path = _approvals_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(approvals, indent=2) + "\n", encoding="utf-8")
