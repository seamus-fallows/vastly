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
    "sshUser": "root",
    "workspace": "/workspace",
    "disableAutoTmux": True,
    "gitRemote": "origin",
}


def load_config(path: Path | None = None) -> dict:
    """Load config from disk, creating from template if missing.

    Returns a dict with all keys populated (user values override defaults).
    """
    path = path or CONFIG_PATH

    if not path.exists():
        template = resources.files("vastly.data").joinpath(".vastly.template.json")
        path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Created config at {path}")
        print("Edit it to change IDE, SSH key, port forwarding, and more.")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(red(f"Invalid JSON in {path}: {e}"))
        print("Fix the file or delete it to regenerate from template.")
        sys.exit(1)

    port_forwards = raw.get("portForwards")
    if port_forwards is None:
        port_forwards = [{"local": 8080, "remote": 8080}]

    post_install = raw.get("postInstall")
    if isinstance(post_install, str):
        post_install = [post_install]
    elif not post_install:
        post_install = []

    return {
        "ide": raw.get("ide") or DEFAULTS["ide"],
        "sshKeyPath": raw.get("sshKeyPath"),
        "sshUser": raw.get("sshUser") or DEFAULTS["sshUser"],
        "portForwards": list(port_forwards),
        "workspace": raw.get("workspace") or DEFAULTS["workspace"],
        "disableAutoTmux": raw.get("disableAutoTmux", DEFAULTS["disableAutoTmux"]),
        "gitRemote": raw.get("gitRemote") or DEFAULTS["gitRemote"],
        "postInstall": list(post_install),
        "installCommand": raw.get("installCommand"),
    }
