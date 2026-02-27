"""CLI entry point for the `vst` command."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import vastly
from vastly import __version__, green, red, yellow
from vastly.config import load_config
from vastly.errors import VastlyError
from vastly.ide import check_ide, open_ide
from vastly.instance import get_synced_instances, select_instance, show_table
from vastly.remote import setup_instances


def _git_root() -> Path | None:
    """Return the root of the current git repo, or None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return Path(top) if top else None


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
        other = {"code": "cursor", "cursor": "code"}.get(ide)
        if other and check_ide(other):
            print(red(f"Missing: {ide}, but {other} is installed."))
            print(red(f'  Update "ide" in ~/.vastly.json to "{other}" to use it.'))
        else:
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
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if stderr and "not a git repository" not in stderr.lower():
            print(red(f"git: {stderr}"), file=sys.stderr)
        return None
    repo_url = result.stdout.strip()
    if not repo_url:
        return None
    repo_name = repo_url.rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
    return repo_url, repo_name


# ── Subcommand handlers ──────────────────────────────────────────────


def _cmd_connect(args) -> None:
    """Connect flow -- sync instances, check setup, run setup if needed, open IDE."""
    git_root = _git_root()
    config = load_config(project_dir=git_root)

    if not _check_prerequisites(need_ide=True, ide=config["ide"]):
        return

    instances = get_synced_instances(config)
    show_table(instances)

    selected = select_instance(instances, args.name)
    if not selected:
        return

    repo_info = _local_repo_info(config["gitRemote"])

    if args.no_setup or not repo_info:
        if not repo_info and not args.no_setup:
            print(
                yellow(
                    "  Not in a git repo. Tip: run vst from inside a git repo to auto-setup."
                )
            )
        for inst in selected:
            print(green(f"  Opening {config['workspace']}"))
            open_ide(config["ide"], inst["name"], config["workspace"])
        return

    repo_url, repo_name = repo_info
    remote_path = f"{config['workspace']}/{repo_name}"

    success_names = setup_instances(
        selected,
        repo_url,
        repo_name,
        config,
        force_setup=args.force_setup,
        project_dir=git_root,
        copy_files=config["copyFiles"],
    )
    for inst_name in success_names:
        print(green(f"  Opening {remote_path}"))
        open_ide(config["ide"], inst_name, remote_path)

    # Only check for updates after successful connect
    from vastly.update import check_for_update

    check_for_update()


def _cmd_list(args) -> None:
    """List running instances."""
    git_root = _git_root()
    config = load_config(project_dir=git_root)

    if not _check_prerequisites(ide=config["ide"]):
        return

    instances = get_synced_instances(config)
    show_table(instances)


# ── Entry point ──────────────────────────────────────────────────────


def main() -> None:
    """Parse arguments and dispatch to the appropriate subcommand."""
    parser = argparse.ArgumentParser(
        prog="vst",
        description="Connect to Vast.ai instances: sync SSH, set up your project, and open your IDE.",
        epilog=(
            "commands: connect, list\n"
            "run vst <command> --help for details\n\n"
            "alias:         `vastly` and `vst` are the same command\n"
            "prerequisites: vastai CLI (pip install vastai), git, ssh, VS Code or Cursor\n"
            "config:        ~/.vastly.json (created on first run)\n"
            "docs:          https://github.com/seamus-fallows/vastly"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"vastly {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    # Connect (default when no subcommand given)
    connect_parser = subparsers.add_parser("connect", help="connect to an instance and open IDE")
    connect_parser.add_argument("name", nargs="?", help="instance name")
    connect_parser.add_argument("-v", "--verbose", action="store_true")
    setup_group = connect_parser.add_mutually_exclusive_group()
    setup_group.add_argument(
        "-n", "--no-setup", action="store_true",
        help="open IDE without cloning or installing",
    )
    setup_group.add_argument(
        "-f", "--force-setup", action="store_true",
        help="re-run remote setup even if already done",
    )

    # List
    list_parser = subparsers.add_parser("list", help="list running instances")
    list_parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    # No subcommand = connect with auto-select
    if args.command is None:
        args.command = "connect"
        args.name = None
        args.no_setup = False
        args.force_setup = False
        args.verbose = False

    if hasattr(args, "verbose") and args.verbose:
        vastly.VERBOSE = True

    try:
        if args.command == "connect":
            _cmd_connect(args)
        elif args.command == "list":
            _cmd_list(args)
    except KeyboardInterrupt:
        print(file=sys.stderr)  # clean up partial line
        sys.exit(130)  # standard exit code for SIGINT
    except VastlyError as e:
        print(red(str(e)), file=sys.stderr)
        sys.exit(e.exit_code)
