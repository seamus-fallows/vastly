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
from vastly.instance import (
    get_synced_instances,
    load_aliases,
    save_aliases,
    select_instance,
    show_table,
    validate_alias,
)
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


def _confirm(prompt: str) -> bool:
    """Ask the user for y/N confirmation. Default is No."""
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == "y"


def _select_single_instance(
    instances: list[dict], name: str | None
) -> dict:
    """Select exactly one instance by name, auto-select, or picker.

    Unlike select_instance which can return multiple via 'all', this
    always returns a single instance dict.
    """
    if name:
        match = [i for i in instances if i["name"] == name or i.get("alias") == name]
        if not match:
            from vastly.instance import _display_name
            labels = [_display_name(i) for i in instances]
            raise VastlyError(f"No instance named '{name}'. Available: {', '.join(labels)}")
        return match[0]

    if len(instances) == 1:
        return instances[0]

    # Interactive selection (no "all" option)
    names = [i["name"] for i in instances]
    print("Select instance:")
    for i, n in enumerate(names):
        print(f"  [{i + 1}] {n}")

    try:
        choice = input("Choice: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise VastlyError("No instance selected.")

    if not choice.isdigit():
        raise VastlyError("No instance selected.")

    idx = int(choice)
    if idx < 1 or idx > len(names):
        raise VastlyError("No instance selected.")

    return instances[idx - 1]


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


def _cmd_name(args) -> None:
    """Assign or remove a custom alias for an instance."""
    if args.clear:
        # Remove alias by name
        aliases = load_aliases()
        found = False
        for inst_id, alias in list(aliases.items()):
            if alias == args.alias:
                del aliases[inst_id]
                found = True
                break
        if not found:
            raise VastlyError(f"No alias '{args.alias}' found.")
        save_aliases(aliases)
        print(green(f"  Removed alias '{args.alias}'"))
        return

    git_root = _git_root()
    config = load_config(project_dir=git_root)

    if not _check_prerequisites(ide=config["ide"]):
        return

    instances = get_synced_instances(config)
    aliases = load_aliases()

    validate_alias(args.alias, instances, aliases)

    inst = _select_single_instance(instances, args.instance)
    inst_id = str(inst["id"])

    # Remove any existing alias for this instance
    if inst_id in aliases:
        old_alias = aliases[inst_id]
        del aliases[inst_id]

    aliases[inst_id] = args.alias
    save_aliases(aliases)
    print(green(f"  Named {inst['name']} as '{args.alias}'"))


def _cmd_stop(args) -> None:
    """Stop one or more running instances."""
    if args.name and args.all:
        raise VastlyError("Cannot specify both an instance name and --all")

    git_root = _git_root()
    config = load_config(project_dir=git_root)

    if not _check_prerequisites(ide=config["ide"]):
        return

    instances = get_synced_instances(config)

    if args.all:
        if not _confirm(f"Stop {len(instances)} instances?"):
            return
        for inst in instances:
            _vastai_action("stop", inst)
        return

    inst = _select_single_instance(instances, args.name)
    _vastai_action("stop", inst)


def _cmd_destroy(args) -> None:
    """Destroy one or more instances (irreversible)."""
    if args.name and args.all:
        raise VastlyError("Cannot specify both an instance name and --all")

    git_root = _git_root()
    config = load_config(project_dir=git_root)

    if not _check_prerequisites(ide=config["ide"]):
        return

    instances = get_synced_instances(config)

    if args.all:
        if not _confirm(f"Destroy {len(instances)} instances? This is irreversible."):
            return
        for inst in instances:
            _vastai_destroy(inst, config)
        return

    inst = _select_single_instance(instances, args.name)
    if not _confirm(f"Destroy {inst['name']}? This is irreversible."):
        return
    _vastai_destroy(inst, config)


def _vastai_action(action: str, inst: dict) -> None:
    """Run 'vastai stop/destroy instance <id>' and print result."""
    inst_id = inst.get("id")
    if not inst_id:
        raise VastlyError(f"Cannot {action} cached instance '{inst['name']}' (no instance ID)")

    result = subprocess.run(
        ["vastai", action, "instance", str(inst_id)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise VastlyError(f"Failed to {action} {inst['name']}: {msg}")

    action_past = "Stopped" if action == "stop" else "Destroyed"
    print(green(f"  {action_past} {inst['name']}"))


def _vastai_destroy(inst: dict, config: dict) -> None:
    """Destroy an instance and clean up its SSH config and alias."""
    from vastly.ssh import SSH_CONFIG_DIR

    _vastai_action("destroy", inst)

    # Clean up SSH config for the destroyed instance
    config_file = SSH_CONFIG_DIR / inst["name"]
    if config_file.exists():
        config_file.unlink()

    # Clean up alias SSH config and alias entry
    inst_id = str(inst.get("id", ""))
    if inst_id:
        aliases = load_aliases()
        alias = aliases.pop(inst_id, None)
        if alias:
            save_aliases(aliases)
            alias_config = SSH_CONFIG_DIR / alias
            if alias_config.exists():
                alias_config.unlink()


def _cmd_cp(args) -> None:
    """Copy files to/from a remote instance."""
    import shlex
    from pathlib import PurePosixPath

    from vastly.ssh import run_scp, run_ssh

    git_root = _git_root()
    if not git_root:
        raise VastlyError("Not in a git repo. vst cp requires a git repo to resolve paths.")

    config = load_config(project_dir=git_root)

    if not _check_prerequisites(ide=config["ide"]):
        return

    repo_info = _local_repo_info(config["gitRemote"])
    if not repo_info:
        raise VastlyError("Could not determine repo name from git remote.")
    _, repo_name = repo_info

    instances = get_synced_instances(config)
    inst = _select_single_instance(instances, args.instance)

    remote_base = f"{config['workspace']}/{repo_name}"
    rel_path = args.path.rstrip("/\\")
    remote_path = f"{remote_base}/{rel_path}"
    local_path = git_root / rel_path

    # Detect if path is a directory (for recursive copy)
    is_dir = args.path.endswith("/") or args.path.endswith("\\")
    if args.direction == "up" and local_path.exists():
        is_dir = is_dir or local_path.is_dir()

    if args.direction == "down":
        # Remote -> local
        local_path.parent.mkdir(parents=True, exist_ok=True)
        src = f"{inst['name']}:{remote_path}"
        if is_dir:
            src = f"{inst['name']}:{remote_path}/"
        result = run_scp(src, str(local_path), recursive=is_dir)
        if result.returncode != 0:
            msg = result.stderr.strip() or "unknown error"
            raise VastlyError(f"Download failed: {msg}")
        print(green(f"  Downloaded {rel_path}"))
    else:
        # Local -> remote: ensure parent directory exists on remote
        if not local_path.exists():
            raise VastlyError(f"Local path not found: {rel_path}")
        parent_rel = str(PurePosixPath(rel_path).parent)
        if parent_rel != ".":
            remote_parent = f"{remote_base}/{parent_rel}"
            run_ssh(inst["name"], f"mkdir -p {shlex.quote(remote_parent)}")
        dest = f"{inst['name']}:{remote_path}"
        result = run_scp(str(local_path), dest, recursive=is_dir)
        if result.returncode != 0:
            msg = result.stderr.strip() or "unknown error"
            raise VastlyError(f"Upload failed: {msg}")
        print(green(f"  Uploaded {rel_path}"))


# ── Entry point ──────────────────────────────────────────────────────


def main() -> None:
    """Parse arguments and dispatch to the appropriate subcommand."""
    parser = argparse.ArgumentParser(
        prog="vst",
        description="Connect to Vast.ai instances: sync SSH, set up your project, and open your IDE.",
        epilog=(
            "commands: connect, list, stop, destroy, name, cp\n"
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

    # Stop
    stop_parser = subparsers.add_parser("stop", help="stop a running instance")
    stop_parser.add_argument("name", nargs="?", help="instance name")
    stop_parser.add_argument("--all", action="store_true", help="stop all running instances")
    stop_parser.add_argument("-v", "--verbose", action="store_true")

    # Destroy
    destroy_parser = subparsers.add_parser("destroy", help="destroy an instance (irreversible)")
    destroy_parser.add_argument("name", nargs="?", help="instance name")
    destroy_parser.add_argument("--all", action="store_true", help="destroy all instances")
    destroy_parser.add_argument("-v", "--verbose", action="store_true")

    # Cp
    cp_parser = subparsers.add_parser("cp", help="copy files to/from a remote instance")
    cp_parser.add_argument("direction", choices=["up", "down"])
    cp_parser.add_argument("path", help="relative file path")
    cp_parser.add_argument(
        "-i", "--instance", help="instance name (default: auto-select or picker)",
    )
    cp_parser.add_argument("-v", "--verbose", action="store_true")

    # Name
    name_parser = subparsers.add_parser("name", help="assign a custom name to an instance")
    name_parser.add_argument("alias", help="custom name to assign")
    name_parser.add_argument(
        "-i", "--instance", help="instance to name (default: auto-select or picker)",
    )
    name_parser.add_argument("--clear", action="store_true", help="remove the alias")
    name_parser.add_argument("-v", "--verbose", action="store_true")

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
        elif args.command == "stop":
            _cmd_stop(args)
        elif args.command == "destroy":
            _cmd_destroy(args)
        elif args.command == "name":
            _cmd_name(args)
        elif args.command == "cp":
            _cmd_cp(args)
    except KeyboardInterrupt:
        print(file=sys.stderr)  # clean up partial line
        sys.exit(130)  # standard exit code for SIGINT
    except VastlyError as e:
        print(red(str(e)), file=sys.stderr)
        sys.exit(e.exit_code)
