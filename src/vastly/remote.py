"""Remote project setup -- SCP setup script and execute."""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from importlib import resources
from pathlib import Path

from vastly import __version__, cyan, green, red, yellow
from vastly.ssh import run_scp, run_ssh


def _setup_script_path() -> Path:
    """Return the path to the bundled setup-remote.sh."""
    ref = resources.files("vastly.data").joinpath("setup-remote.sh")
    return Path(str(ref))


def convert_to_ssh_url(url: str) -> str:
    """Convert an HTTPS git URL to SSH format."""
    m = re.match(r"https://([^/]+)/(.+?)/?$", url)
    if m:
        return f"git@{m.group(1)}:{m.group(2)}"
    return url


def setup_instances(
    instances: list[dict],
    repo_url: str,
    repo_name: str,
    config: dict,
) -> list[str]:
    """Run remote setup on each instance. Returns list of successful host names."""
    git_name = subprocess.run(
        ["git", "config", "--global", "user.name"],
        capture_output=True, text=True,
    ).stdout.strip()

    git_email = subprocess.run(
        ["git", "config", "--global", "user.email"],
        capture_output=True, text=True,
    ).stdout.strip()

    if not git_name or not git_email:
        print(red('Git identity not configured. Run: git config --global user.name "Your Name"'))
        return []

    setup_script = _setup_script_path()
    if not setup_script.exists():
        print(red(f"Setup script not found at {setup_script}"))
        return []

    install_cmd = config["installCommand"] or "auto"
    disable_tmux = "true" if config["disableAutoTmux"] else "false"

    success_names = []

    for inst in instances:
        name = inst["name"]
        print(f"  {name}: ", end="", flush=True)

        # SSH retry loop -- instances may still be booting
        reachable = False
        for attempt in range(1, 4):
            result = run_ssh(name, "echo ok")
            if result.returncode == 0:
                reachable = True
                break
            if attempt < 3:
                print(yellow("waiting..."), end="", flush=True)
                time.sleep(5)

        if not reachable:
            print(red("unreachable after 3 attempts. Check vastai show instances to confirm it is running."))
            continue

        # Check setup marker
        marker_result = run_ssh(name, f"test -f ~/.vastly/setup/{repo_name}.json && echo done")
        if marker_result.stdout.strip() == "done":
            print(green("already set up."))
            success_names.append(name)
            continue

        print(cyan("running setup..."))

        # SCP setup script
        scp_result = run_scp(str(setup_script), f"{name}:/tmp/_vastly-setup.sh", setup=True)
        if scp_result.returncode != 0:
            print(red(f"  {name}: failed to copy setup script"))
            continue

        # Build argument list
        setup_args = [
            repo_url, repo_name, git_name, git_email,
            config["workspace"], disable_tmux, install_cmd, __version__,
        ] + config["postInstall"]

        quoted = " ".join(shlex.quote(a) for a in setup_args)
        remote_cmd = (
            "sed -i 's/\\r$//' /tmp/_vastly-setup.sh && "
            f"bash /tmp/_vastly-setup.sh {quoted}; "
            "e=$?; rm -f /tmp/_vastly-setup.sh; exit $e"
        )

        result = run_ssh(name, remote_cmd, setup=True, stream=True)

        if result.returncode != 0:
            print(red(f"  {name}: setup failed (exit {result.returncode})"))
            continue

        print(green(f"  {name}: done."))
        success_names.append(name)

    return success_names
