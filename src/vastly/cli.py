"""CLI entry point for the `vst` command."""

from __future__ import annotations

import argparse
import shutil
import subprocess

from vastly import __version__
from vastly.config import load_config
from vastly.ide import check_ide, open_ide
from vastly.instance import get_synced_instances, select_instance, show_table
from vastly.remote import convert_to_ssh_url, setup_instances
from vastly.ssh import run_ssh


def _check_prerequisites(*, need_ide: bool = False, ide: str) -> bool:
    """Verify required tools are available."""
    ok = True

    if not shutil.which("vastai"):
        print("\033[31mMissing: vastai CLI. Install with: pip install vastai\033[0m")
        ok = False
    if not shutil.which("git"):
        print("\033[31mMissing: git. Install from https://git-scm.com\033[0m")
        ok = False
    if not shutil.which("ssh"):
        print("\033[31mMissing: ssh.\033[0m")
        ok = False
    if need_ide and not check_ide(ide):
        urls = {"code": "https://code.visualstudio.com", "cursor": "https://cursor.com"}
        url = urls.get(ide, "")
        hint = f" Download from {url}" if url else ""
        print(f"\033[31mMissing: {ide}.{hint}\033[0m")
        ok = False

    return ok


def _local_repo_info(git_remote: str) -> tuple[str, str] | None:
    """Return (repo_url, repo_name) from the local git repo, or None."""
    try:
        repo_url = subprocess.run(
            ["git", "remote", "get-url", git_remote],
            capture_output=True, text=True,
        ).stdout.strip()
    except FileNotFoundError:
        return None
    if not repo_url:
        return None
    repo_url = convert_to_ssh_url(repo_url)
    repo_name = repo_url.rsplit("/", 1)[-1].removesuffix(".git")
    return repo_url, repo_name


def _connect(name: str | None, no_setup: bool) -> None:
    """Main connect flow -- sync instances, check setup, run setup if needed, open IDE."""
    config = load_config()

    if not _check_prerequisites(need_ide=True, ide=config["ide"]):
        return

    instances = get_synced_instances(config)
    if not instances:
        return

    show_table(instances)

    selected = select_instance(instances, name)
    if not selected:
        return

    repo_info = _local_repo_info(config["gitRemote"])

    for inst in selected:
        inst_name = inst["name"]

        if no_setup or not repo_info:
            # Skip setup -- open workspace root
            if not repo_info and not no_setup:
                print(f"  \033[33mNot in a git repo. Tip: run vst from inside a git repo to auto-setup.\033[0m")
            print(f"  \033[32mOpening {config['workspace']}\033[0m")
            open_ide(config["ide"], inst_name, config["workspace"])
            continue

        repo_url, repo_name = repo_info
        remote_path = f"{config['workspace']}/{repo_name}"

        # Check if project is already set up via marker file
        result = run_ssh(inst_name, f"test -f ~/.vastly/setup/{repo_name}.json && echo done")

        if result.returncode != 0:
            print(f"  \033[31m{inst_name}: unreachable via SSH.\033[0m")
            continue

        if result.stdout.strip() == "done":
            print(f"  \033[32mOpening {remote_path}\033[0m")
            open_ide(config["ide"], inst_name, remote_path)
            continue

        # Not set up yet -- run setup
        success = setup_instances([inst], repo_url, repo_name, config)
        if success:
            open_ide(config["ide"], inst_name, remote_path)


def main() -> None:
    """Parse arguments and run the connect flow."""
    parser = argparse.ArgumentParser(
        prog="vst",
        description="Connect to a Vast.ai instance -- sync SSH, set up your project, and open your IDE.",
        epilog=(
            "prerequisites: vastai CLI (pip install vastai), git, ssh, VS Code or Cursor\n"
            "config:        ~/.vastly.json (created on first run)\n"
            "docs:          https://github.com/seamus-fallows/vastly"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("name", nargs="?", help="instance name (tab-complete from ~/.ssh/vast.d/)")
    parser.add_argument("--no-setup", action="store_true", help="open IDE without cloning or installing")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    args = parser.parse_args()
    _connect(args.name, args.no_setup)
