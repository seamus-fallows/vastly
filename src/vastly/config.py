"""Load and manage ~/.vastly.json configuration."""

from __future__ import annotations

import json
import os
import shutil
import sys
from importlib import resources
from pathlib import Path

import vastly
from vastly.errors import ConfigError

CONFIG_PATH = Path.home() / ".vastly.json"

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
}

_STRING_KEYS = {"ide", "sshUser", "workspace", "gitRemote"}
_PROJECT_KEYS = {
    "postInstall",
    "installCommand",
    "workspace",
    "portForwards",
    "copyFiles",
    "gitRemote",
}


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


_KNOWN_KEYS = set(DEFAULTS)


def _validate_config(config: dict) -> None:
    """Validate types and basic shapes of a resolved config dict.

    Raises ConfigError with a clear message on the first problem found.
    """
    # --- non-empty string keys ---
    for key in ("ide", "sshUser", "workspace", "gitRemote"):
        val = config.get(key)
        if not isinstance(val, str) or not val:
            raise ConfigError(
                f"Invalid config: '{key}' must be a non-empty string, "
                f"got {type(val).__name__}"
            )

    # workspace must start with /
    if not config["workspace"].startswith("/"):
        raise ConfigError(
            f"Invalid config: 'workspace' must start with '/', "
            f"got {config['workspace']!r}"
        )

    # sshKeyPath: None or non-empty string
    skp = config.get("sshKeyPath")
    if skp is not None and (not isinstance(skp, str) or not skp):
        raise ConfigError(
            f"Invalid config: 'sshKeyPath' must be None or a non-empty string, "
            f"got {type(skp).__name__}"
        )

    # disableAutoTmux: bool
    dat = config.get("disableAutoTmux")
    if not isinstance(dat, bool):
        raise ConfigError(
            f"Invalid config: 'disableAutoTmux' must be a bool, "
            f"got {type(dat).__name__}"
        )

    # installCommand: None or non-empty string
    ic = config.get("installCommand")
    if ic is not None and (not isinstance(ic, str) or not ic):
        raise ConfigError(
            f"Invalid config: 'installCommand' must be None or a non-empty string, "
            f"got {type(ic).__name__}"
        )

    # portForwards: list of dicts with int local and int remote
    pf = config.get("portForwards")
    if not isinstance(pf, list):
        raise ConfigError(
            f"Invalid config: 'portForwards' must be a list of {{local, remote}} objects, "
            f"got {type(pf).__name__}"
        )
    for i, entry in enumerate(pf):
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

    # postInstall: list of strings
    pi = config.get("postInstall")
    if not isinstance(pi, list):
        raise ConfigError(
            f"Invalid config: 'postInstall' must be a list of strings, "
            f"got {type(pi).__name__}"
        )
    for i, entry in enumerate(pi):
        if not isinstance(entry, str):
            raise ConfigError(
                f"Invalid config: 'postInstall[{i}]' must be a string, "
                f"got {type(entry).__name__}"
            )

    # copyFiles: list of strings
    cf = config.get("copyFiles")
    if not isinstance(cf, list):
        raise ConfigError(
            f"Invalid config: 'copyFiles' must be a list of strings, "
            f"got {type(cf).__name__}"
        )
    for i, entry in enumerate(cf):
        if not isinstance(entry, str):
            raise ConfigError(
                f"Invalid config: 'copyFiles[{i}]' must be a string, "
                f"got {type(entry).__name__}"
            )


def _warn_unknown_keys(raw: dict, source: str) -> None:
    """Print a warning to stderr for any unrecognized top-level keys."""
    unknown = set(raw) - _KNOWN_KEYS
    if unknown:
        keys = ", ".join(sorted(unknown))
        print(f"Warning: unrecognized config keys in {source}: {keys}", file=sys.stderr)


def load_config(path: Path | None = None, *, project_dir: Path | None = None) -> dict:
    """Load config from disk, creating from template if missing.

    If ``project_dir`` is given and contains a ``.vastly.json``, project-specific
    keys (postInstall, installCommand, workspace, portForwards, copyFiles,
    gitRemote) are overlaid on the global config.  User-specific keys (ide,
    sshKeyPath, sshUser, disableAutoTmux) in a project config are silently
    ignored.
    """
    path = path or CONFIG_PATH

    if not path.exists():
        template = resources.files("vastly.data").joinpath(".vastly.template.json")
        config_data = json.loads(template.read_text(encoding="utf-8"))
        config_data["ide"] = _detect_ide()
        path.write_text(json.dumps(config_data, indent=2) + "\n", encoding="utf-8")
        print(f"Created config at {path}\n")
        print("Run 'vst' from a git repo to connect to your Vast.ai instance.")
        print("It syncs SSH, clones your repo, installs deps, and opens your IDE.")
        print("Run 'vst -h' for all commands.")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"Invalid JSON in {path}: {e}\n"
            "Fix the file or delete it to regenerate from template."
        ) from e

    _warn_unknown_keys(raw, str(path))

    config = {}
    for k, v in DEFAULTS.items():
        user_val = raw.get(k)
        if user_val is None or (k in _STRING_KEYS and user_val == ""):
            config[k] = v
        else:
            config[k] = user_val

    # Auto-detect IDE from terminal environment (overrides config when inside an IDE).
    env_ide = _ide_from_env()
    if env_ide:
        config["ide"] = env_ide

    if isinstance(config["postInstall"], str):
        config["postInstall"] = [config["postInstall"]]

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

            for k, v in project_raw.items():
                if k not in _PROJECT_KEYS:
                    continue
                if v is None or (k in _STRING_KEYS and v == ""):
                    continue
                config[k] = v

            if isinstance(config["postInstall"], str):
                config["postInstall"] = [config["postInstall"]]

    _validate_config(config)
    vastly.verbose(f"Config loaded from {path}")
    return config
