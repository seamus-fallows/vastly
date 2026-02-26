"""Load and manage ~/.vastly.json configuration."""

from __future__ import annotations

import json
import os
import shutil
import sys
from importlib import resources
from pathlib import Path

from vastly import red

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
_PROJECT_KEYS = {"postInstall", "installCommand", "workspace", "portForwards", "copyFiles", "gitRemote"}


def _detect_ide() -> str:
    """Detect which IDE the user likely wants.

    Checks TERM_PROGRAM first (set by IDE integrated terminals),
    then falls back to what's installed.
    """
    term = os.environ.get("TERM_PROGRAM", "").lower()
    if "cursor" in term:
        return "cursor"
    if "vscode" in term:
        return "code"

    # TERM_PROGRAM not set (e.g. external terminal on Windows) -- check what's installed
    has_code = shutil.which("code") is not None
    has_cursor = shutil.which("cursor") is not None
    if has_cursor and not has_code:
        return "cursor"
    return "code"


def load_config(
    path: Path | None = None, *, project_dir: Path | None = None
) -> dict:
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
        print(f"Created config at {path}")
        print("Edit it to change IDE, SSH key, port forwarding, and more.")
        print("Tip: add a .vastly.json to any repo for project-specific settings.")
        print("Tip: you can also run this command as `vst` for short.")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(red(f"Invalid JSON in {path}: {e}"))
        print("Fix the file or delete it to regenerate from template.")
        sys.exit(1)

    config = {}
    for k, v in DEFAULTS.items():
        user_val = raw.get(k)
        if user_val is None or (k in _STRING_KEYS and user_val == ""):
            config[k] = v
        else:
            config[k] = user_val

    # Auto-detect IDE from terminal environment (overrides config when inside an IDE).
    # Only override if the detected IDE is actually installed -- TERM_PROGRAM can be
    # inherited from a parent process even when the CLI isn't available.
    term = os.environ.get("TERM_PROGRAM", "").lower()
    if "cursor" in term and shutil.which("cursor"):
        config["ide"] = "cursor"
    elif "vscode" in term and shutil.which("code"):
        config["ide"] = "code"

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
                print(red(f"Invalid JSON in {project_cfg}: {e}"))
                print("Fix the project config or remove it.")
                sys.exit(1)

            for k, v in project_raw.items():
                if k not in _PROJECT_KEYS:
                    continue
                if v is None or (k in _STRING_KEYS and v == ""):
                    continue
                config[k] = v

            if isinstance(config["postInstall"], str):
                config["postInstall"] = [config["postInstall"]]

    return config
