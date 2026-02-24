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


def _connect(name: str | None, no_setup: bool) -> None:
    """Main connect flow -- sync instances, detect projects, setup if needed, open IDE."""
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

    for inst in selected:
        inst_name = inst["name"]
        print(f"\033[90mChecking {inst_name}...\033[0m")

        # List non-hidden directories in workspace
        result = run_ssh(
            inst_name,
            f"find {config['workspace']} -mindepth 1 -maxdepth 1 -type d -not -name '.*' -printf '%f\\n'",
        )

        if result.returncode != 0:
            print(f"  \033[31m{inst_name}: unreachable via SSH. Check vastai show instances to confirm it is running.\033[0m")
            continue

        dir_list = [d for d in result.stdout.strip().splitlines() if d]

        if len(dir_list) == 1:
            remote_path = f"{config['workspace']}/{dir_list[0]}"
            print(f"  \033[32mOpening {remote_path}\033[0m")
            open_ide(config["ide"], inst_name, remote_path)

        elif len(dir_list) > 1:
            print(f"  Multiple projects found on {inst_name}:")
            for i, d in enumerate(dir_list):
                print(f"    [{i + 1}] {d}")
            print(f"    [0] Open {config['workspace']}")

            try:
                pick = input("  Choice: ").strip()
            except (EOFError, KeyboardInterrupt):
                continue

            if not pick.isdigit():
                continue

            pick = int(pick)
            if pick == 0:
                remote_path = config["workspace"]
            elif 1 <= pick <= len(dir_list):
                remote_path = f"{config['workspace']}/{dir_list[pick - 1]}"
            else:
                continue

            open_ide(config["ide"], inst_name, remote_path)

        else:
            # No projects found on remote
            if no_setup:
                print(f"  \033[32mOpening {config['workspace']}\033[0m")
                open_ide(config["ide"], inst_name, config["workspace"])
                continue

            try:
                repo_url = subprocess.run(
                    ["git", "remote", "get-url", config["gitRemote"]],
                    capture_output=True, text=True,
                ).stdout.strip()
            except FileNotFoundError:
                repo_url = ""

            if not repo_url:
                print(f"  \033[33mNo projects on remote and not in a local git repo.\033[0m")
                print(f"  \033[90mOpening {config['workspace']}. Tip: run vst from inside a git repo to auto-setup.\033[0m")
                open_ide(config["ide"], inst_name, config["workspace"])
                continue

            repo_url = convert_to_ssh_url(repo_url)
            repo_name = repo_url.rsplit("/", 1)[-1].removesuffix(".git")

            success = setup_instances([inst], repo_url, repo_name, config)
            if success:
                open_ide(config["ide"], inst_name, f"{config['workspace']}/{repo_name}")


def main() -> None:
    """Parse arguments and run the connect flow."""
    parser = argparse.ArgumentParser(
        prog="vst",
        description="Connect to a Vast.ai instance -- sync SSH, provision, and open your IDE.",
        epilog=(
            "prerequisites: vastai CLI (pip install vastai), git, ssh, VS Code or Cursor\n"
            "config:        ~/.vastly.json (created on first run)\n"
            "docs:          https://github.com/seamusfallows/vastly"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("name", nargs="?", help="instance name (tab-complete from ~/.ssh/vast.d/)")
    parser.add_argument("--no-setup", action="store_true", help="open IDE without cloning or installing")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    args = parser.parse_args()
    _connect(args.name, args.no_setup)
