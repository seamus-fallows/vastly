"""Load and manage ~/.vastly.json configuration."""

from __future__ import annotations

import json
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
    "disableAutoTmux": True,
    "gitRemote": "origin",
    "postInstall": [],
    "installCommand": None,
}

_STRING_KEYS = {"ide", "sshUser", "workspace", "gitRemote"}


def load_config(path: Path | None = None) -> dict:
    """Load config from disk, creating from template if missing."""
    path = path or CONFIG_PATH

    if not path.exists():
        template = resources.files("vastly.data").joinpath(".vastly.template.json")
        path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Created config at {path}")
        print("Edit it to change IDE, SSH key, port forwarding, and more.")
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

    if isinstance(config["postInstall"], str):
        config["postInstall"] = [config["postInstall"]]

    return config
