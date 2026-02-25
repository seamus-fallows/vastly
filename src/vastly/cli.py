"""CLI entry point for the `vst` command."""

from __future__ import annotations

import argparse
import shutil
import subprocess

from vastly import __version__, green, red, yellow
from vastly.config import load_config
from vastly.ide import check_ide, open_ide
from vastly.instance import get_synced_instances, select_instance, show_table
from vastly.remote import convert_to_ssh_url, setup_instances


def _check_prerequisites(*, need_ide: bool = False, ide: str) -> bool:
    """Verify required tools are available."""
    ok = True

    if not shutil.which("vastai"):
        print(red("Missing: vastai CLI. Install with: pip install vastai"))
        ok = False
    if not shutil.which("git"):
        print(red("Missing: git. Install from https://git-scm.com"))
        ok = False
    if not shutil.which("ssh"):
        print(red("Missing: ssh."))
        ok = False
    if need_ide and not check_ide(ide):
        urls = {"code": "https://code.visualstudio.com", "cursor": "https://cursor.com"}
        url = urls.get(ide, "")
        hint = f" Download from {url}" if url else ""
        print(red(f"Missing: {ide}.{hint}"))
        ok = False

    return ok


def _local_repo_info(git_remote: str) -> tuple[str, str] | None:
    """Return (repo_url, repo_name) from the local git repo, or None."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", git_remote],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if stderr and "not a git repository" not in stderr.lower():
            print(red(f"git: {stderr}"))
        return None
    repo_url = result.stdout.strip()
    if not repo_url:
        return None
    repo_url = convert_to_ssh_url(repo_url)
    repo_name = repo_url.rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
    return repo_url, repo_name


def _connect(name: str | None, *, no_setup: bool, force_setup: bool, list_only: bool) -> None:
    """Main connect flow -- sync instances, check setup, run setup if needed, open IDE."""
    config = load_config()

    if not _check_prerequisites(need_ide=not list_only, ide=config["ide"]):
        return

    instances = get_synced_instances(config)
    if not instances:
        return

    show_table(instances)

    if list_only:
        return

    selected = select_instance(instances, name)
    if not selected:
        return

    repo_info = _local_repo_info(config["gitRemote"])

    if no_setup or not repo_info:
        if not repo_info and not no_setup:
            print(yellow("  Not in a git repo. Tip: run vst from inside a git repo to auto-setup."))
        for inst in selected:
            print(green(f"  Opening {config['workspace']}"))
            open_ide(config["ide"], inst["name"], config["workspace"])
        return

    repo_url, repo_name = repo_info
    remote_path = f"{config['workspace']}/{repo_name}"

    success_names = setup_instances(selected, repo_url, repo_name, config, force_setup=force_setup)
    for inst_name in success_names:
        print(green(f"  Opening {remote_path}"))
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
    parser.add_argument("name", nargs="?", help="instance name (from --list output)")
    parser.add_argument("--list", action="store_true", help="list instances and exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    setup_group = parser.add_mutually_exclusive_group()
    setup_group.add_argument("--no-setup", action="store_true", help="open IDE without cloning or installing")
    setup_group.add_argument("--force-setup", action="store_true", help="re-run remote setup even if already done")

    args = parser.parse_args()
    _connect(args.name, no_setup=args.no_setup, force_setup=args.force_setup, list_only=args.list)
