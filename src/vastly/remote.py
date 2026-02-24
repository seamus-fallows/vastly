"""Remote instance provisioning -- SCP setup script and execute."""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from importlib import resources
from pathlib import Path

from vastly import __version__
from vastly.ssh import run_scp, run_ssh


def _setup_script_path() -> Path:
    """Return the path to the bundled setup-remote.sh."""
    ref = resources.files("vastly.data").joinpath("setup-remote.sh")
    return Path(str(ref))


def convert_to_ssh_url(url: str) -> str:
    """Convert a GitHub HTTPS URL to an SSH URL."""
    m = re.match(r"https://github\.com/(.+)", url)
    if m:
        return f"git@github.com:{m.group(1)}"
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
        print('\033[31mGit identity not configured. Run: git config --global user.name "Your Name"\033[0m')
        return []

    setup_script = _setup_script_path()
    if not setup_script.exists():
        print(f"\033[31mSetup script not found at {setup_script}\033[0m")
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
                print("\033[33mwaiting...\033[0m", end="", flush=True)
                time.sleep(5)

        if not reachable:
            print("\033[31munreachable after 3 attempts. Check vastai show instances to confirm it is running.\033[0m")
            continue

        # Check setup marker
        marker_result = run_ssh(name, f"test -f ~/.vastly/setup/{repo_name}.json && echo done")
        if marker_result.stdout.strip() == "done":
            print("\033[32malready set up.\033[0m")
            success_names.append(name)
            continue

        print("\033[36mrunning setup...\033[0m")

        # SCP setup script
        scp_result = run_scp(str(setup_script), f"{name}:/tmp/_vastly-setup.sh", setup=True)
        if scp_result.returncode != 0:
            print(f"  \033[31m{name}: failed to copy setup script\033[0m")
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
            print(f"  \033[31m{name}: setup failed (exit {result.returncode})\033[0m")
            continue

        print(f"  \033[32m{name}: done.\033[0m")
        success_names.append(name)

    return success_names
