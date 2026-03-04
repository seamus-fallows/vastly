"""CLI entry point for the `vst` command."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import vastly
from vastly import __version__, cyan, dim, green, red, yellow
from vastly.config import load_config
from vastly.errors import VastlyError
from vastly.ide import check_ide, open_ide
from vastly.instance import (
    ALREADY_STOPPED_STATES,
    STARTABLE_STATES,
    STOPPABLE_STATES,
    TRANSITIONAL_STATES,
    _display_name,
    get_running_instances,
    get_synced_instances,
    load_aliases,
    save_aliases,
    select_instance,
    show_table,
    sync_instances,
    validate_alias,
)
from vastly.remote import setup_instances
from vastly.ssh import SSH_CONFIG_DIR


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


def _check_prerequisites(*, need_ide: bool = False, ide: str) -> None:
    """Verify required tools are available. Exits with code 1 on failure."""
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
            urls = {
                "code": "https://code.visualstudio.com",
                "cursor": "https://cursor.com",
            }
            url = urls.get(ide, "")
            hint = f" Download from {url}" if url else ""
            print(red(f"Missing: {ide}.{hint}"))
        ok = False

    if not ok:
        sys.exit(1)


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


# ── Subcommand handlers ──────────────────────────────────────────────


def _cmd_connect(args) -> None:
    """Connect flow -- sync instances, check setup, run setup if needed, open IDE."""
    git_root = _git_root()
    config = load_config(project_dir=git_root)

    _check_prerequisites(need_ide=True, ide=config["ide"])

    all_instances = sync_instances(config)
    running = [i for i in all_instances if i.get("status") == "running"]

    if not running:
        # Auto-start a stopped instance instead of erroring
        startable = [i for i in all_instances if i.get("status") in STARTABLE_STATES]

        if args.name:
            match = [
                i
                for i in startable
                if i["name"] == args.name or i.get("alias") == args.name
            ]
            if match:
                inst = match[0]
            else:
                match_all = [
                    i
                    for i in all_instances
                    if i["name"] == args.name or i.get("alias") == args.name
                ]
                if match_all:
                    raise VastlyError(
                        f"'{args.name}' is {match_all[0].get('status', 'unknown')} and cannot be started."
                    )
                raise VastlyError(f"No instance named '{args.name}'.")
        elif not startable:
            raise VastlyError("No Vast instances found.")
        elif len(startable) == 1:
            inst = startable[0]
            print(yellow(f"  No running instances. Starting {_display_name(inst)}..."))
        else:
            print(yellow("  No running instances. Select one to start:"))
            show_table(startable)
            inst = select_instance(startable)[0]

        _vastai_action("start", inst)
        _poll_for_running(str(inst["id"]))

        # Re-sync for fresh SSH configs
        all_instances = sync_instances(config)
        running = [i for i in all_instances if i.get("status") == "running"]
        if not running:
            raise VastlyError(
                "Instance started but not reachable. Check Vast.ai dashboard."
            )

    show_table(running)

    # If the user named a non-running instance, give a specific error
    if args.name:
        match_running = [
            i for i in running if i["name"] == args.name or i.get("alias") == args.name
        ]
        if not match_running:
            match_all = [
                i
                for i in all_instances
                if i["name"] == args.name or i.get("alias") == args.name
            ]
            if match_all:
                status = match_all[0].get("status", "unknown")
                raise VastlyError(
                    f"'{args.name}' is {status}. Use 'vst start {args.name}' to start it."
                )

    selected = select_instance(running, args.name, allow_all=True)

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

    _check_prerequisites(ide=config["ide"])

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

    _check_prerequisites(ide=config["ide"])

    instances = get_running_instances(config)
    aliases = load_aliases()

    validate_alias(args.alias, instances, aliases)

    inst = select_instance(instances, args.instance)[0]
    inst_id = str(inst["id"])

    # Remove any existing alias for this instance (and its SSH config)
    if inst_id in aliases:
        old_alias = aliases[inst_id]
        old_config = SSH_CONFIG_DIR / old_alias
        if old_config.exists():
            old_config.unlink()
        del aliases[inst_id]

    aliases[inst_id] = args.alias
    save_aliases(aliases)
    print(green(f"  Named {inst['name']} as '{args.alias}'"))


def _cmd_stop(args) -> None:
    """Stop one or more running or transitional instances."""
    git_root = _git_root()
    config = load_config(project_dir=git_root)

    _check_prerequisites(ide=config["ide"])

    instances = get_synced_instances(config)

    # Give a specific error if named instance exists but is already stopped
    if args.name:
        match = [
            i
            for i in instances
            if i["name"] == args.name or i.get("alias") == args.name
        ]
        if match and match[0].get("status") in ALREADY_STOPPED_STATES:
            raise VastlyError(f"Already stopped ({match[0].get('status')}).")

    stoppable = [i for i in instances if i.get("status") in STOPPABLE_STATES]
    if not stoppable:
        raise VastlyError("No running instances to stop.")

    if args.all:
        selected = stoppable
    else:
        selected = select_instance(stoppable, args.name, allow_all=True)

    if len(selected) > 1:
        if not _confirm(f"Stop {len(selected)} instances?"):
            return

    for inst in selected:
        _vastai_action("stop", inst)


def _cmd_destroy(args) -> None:
    """Destroy one or more instances (irreversible)."""
    git_root = _git_root()
    config = load_config(project_dir=git_root)

    _check_prerequisites(ide=config["ide"])

    instances = get_synced_instances(config)

    if args.all:
        selected = instances
    else:
        selected = select_instance(instances, args.name, allow_all=True)

    if len(selected) == 1:
        if not _confirm(f"Destroy {_display_name(selected[0])}? This is irreversible."):
            return
    else:
        if not _confirm(f"Destroy {len(selected)} instances? This is irreversible."):
            return

    for inst in selected:
        _vastai_destroy(inst)


def _vastai_action(action: str, inst: dict) -> None:
    """Run 'vastai stop/destroy instance <id>' and print result."""
    inst_id = inst.get("id")
    if not inst_id:
        raise VastlyError(
            f"Cannot {action} cached instance '{inst['name']}' (no instance ID)"
        )

    result = subprocess.run(
        ["vastai", action, "instance", str(inst_id)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise VastlyError(f"Failed to {action} {inst['name']}: {msg}")

    action_past = {"stop": "Stopped", "destroy": "Destroyed", "start": "Started"}.get(
        action, action.capitalize() + "ed"
    )
    print(green(f"  {action_past} {inst['name']}"))


def _vastai_destroy(inst: dict) -> None:
    """Destroy an instance and clean up its SSH config and alias."""
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
        raise VastlyError(
            "Not in a git repo. vst cp requires a git repo to resolve paths."
        )

    config = load_config(project_dir=git_root)

    _check_prerequisites(ide=config["ide"])

    repo_info = _local_repo_info(config["gitRemote"])
    if not repo_info:
        raise VastlyError("Could not determine repo name from git remote.")
    _, repo_name = repo_info

    instances = get_running_instances(config)
    inst = select_instance(instances, args.instance)[0]

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


_START_TIMEOUT = 300  # 5 minutes
_START_POLL_INTERVAL = 5


def _poll_for_running(inst_id: str) -> None:
    """Poll the Vast.ai API until instance is running, or raise on timeout."""
    import time

    deadline = time.monotonic() + _START_TIMEOUT
    last_status = "unknown"

    while time.monotonic() < deadline:
        time.sleep(_START_POLL_INTERVAL)

        result = subprocess.run(
            ["vastai", "show", "instance", inst_id, "--raw"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            continue

        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            continue

        cur_state = data.get("cur_state", "unknown")
        if cur_state == "running":
            print(green("  Instance is running."))
            return

        if cur_state != last_status:
            last_status = cur_state
            hint = (
                " (Ctrl+C to cancel -- scheduling can take a while)"
                if cur_state == "scheduling"
                else ""
            )
            print(dim(f"  Waiting... ({cur_state}){hint}"))

    raise VastlyError(
        "Timed out waiting for instance to start. "
        "Check the Vast.ai dashboard for status."
    )


def _cmd_start(args) -> None:
    """Start a stopped/exited instance, wait for readiness, then connect."""
    git_root = _git_root()
    config = load_config(project_dir=git_root)

    _check_prerequisites(ide=config["ide"])

    instances = get_synced_instances(config)

    # Filter to non-running instances for selection
    non_running = [i for i in instances if i.get("status") != "running"]
    if not non_running:
        raise VastlyError(
            "All instances are already running. Run 'vst' to connect."
        )

    inst = select_instance(non_running, args.name)[0]
    status = inst.get("status", "unknown")

    if status in STARTABLE_STATES:
        _vastai_action("start", inst)
    elif status in TRANSITIONAL_STATES:
        print(dim(f"  Instance is {status}, waiting for it to be ready..."))
    else:
        raise VastlyError(f"Cannot start instance in '{status}' state.")

    if args.no_connect:
        return

    _poll_for_running(str(inst["id"]))

    # Auto-connect: triggers a fresh sync_instances which writes SSH configs
    connect_args = argparse.Namespace(
        command="connect",
        name=inst.get("alias") or inst["name"],
        no_setup=False,
        force_setup=False,
        verbose=getattr(args, "verbose", False),
    )
    _cmd_connect(connect_args)


def _cmd_config(args) -> None:
    """Show resolved configuration."""
    from vastly.config import CONFIG_PATH, _PROJECT_KEYS

    git_root = _git_root()
    config = load_config(project_dir=git_root)

    print(f"\nvastly v{__version__}\n")

    keys = [
        ("ide", "IDE to open (code or cursor)"),
        ("sshKeyPath", "path to SSH private key"),
        ("sshUser", "SSH user on instances"),
        ("portForwards", "ports forwarded to localhost"),
        ("workspace", "remote project directory"),
        ("disableAutoTmux", "prevent auto-tmux on instances"),
        ("gitRemote", "git remote for repo URL"),
        ("postInstall", "commands to run after setup"),
        ("installCommand", "override dependency install"),
        ("copyFiles", "files to copy after setup"),
    ]

    def fmt(key: str, val) -> str:
        if val is None:
            return {"sshKeyPath": "(ssh-agent)", "installCommand": "(auto)"}.get(
                key, "(none)"
            )
        if isinstance(val, bool):
            return str(val).lower()
        if isinstance(val, list):
            if not val:
                return "(none)"
            if key == "portForwards":
                return ", ".join(f"{pf['local']}:{pf['remote']}" for pf in val)
            return json.dumps(val)
        return str(val)

    print(cyan("config:") + f" {CONFIG_PATH}")
    key_w = max(len(k) for k, _ in keys)
    rows = [(k, fmt(k, config.get(k)), desc) for k, desc in keys]
    val_w = max(len(v) for _, v, _ in rows)
    for key, val, desc in rows:
        print(f"  {green(key.ljust(key_w))}  {val.ljust(val_w)}  {dim(desc)}")

    # Project config overlay
    if git_root:
        project_cfg = git_root / ".vastly.json"
        if project_cfg.exists():
            print(f"\n{cyan('project config:')} {project_cfg}")
            try:
                project_raw = json.loads(project_cfg.read_text(encoding="utf-8"))
                for key in project_raw:
                    if key in _PROJECT_KEYS:
                        val = fmt(key, project_raw[key])
                        print(
                            f"  {green(key.ljust(key_w))}  {val.ljust(val_w)}  {dim('(overrides global)')}"
                        )
            except (json.JSONDecodeError, OSError):
                print(dim("  (invalid JSON)"))
        else:
            print(
                f"\n{dim('project config: (none -- add .vastly.json to repo root to override per-project)')}"
            )
    else:
        print(f"\n{dim('project config: (none -- not in a git repo)')}")

    # Tips
    print(f"\n{cyan('tips:')}")
    tips = [
        ("Edit ~/.vastly.json to customize", "'vastly' and 'vst' are the same command"),
        ("Add .vastly.json to any repo", "vst -v for debug output"),
        ("vst -f to re-run setup", "https://github.com/seamus-fallows/vastly"),
    ]
    for left, right in tips:
        print(dim(f"  {left.ljust(36)}{right}"))
    print()


# ── Entry point ──────────────────────────────────────────────────────


_CMD_HELP = {
    "connect": {
        "usage": "vst [name] [-n | -f]",
        "desc": "Connect to an instance and open your IDE.",
        "detail": "First visit: clones your repo, installs deps. Revisits: skips straight to IDE.",
        "examples": [
            ("vst", "auto-select and connect"),
            ("vst train", "connect by alias"),
            ("vst -f", "force re-run setup"),
        ],
    },
    "list": {
        "usage": "vst list",
        "desc": "List all instances with status and cost.",
        "detail": "Syncs with the Vast.ai API each time.",
    },
    "start": {
        "usage": "vst start [name] [-n]",
        "desc": "Start a stopped or exited instance.",
        "detail": "Waits for the instance to be ready, then connects automatically.",
        "examples": [
            ("vst start", "start and connect"),
            ("vst start train", "start by alias"),
            ("vst start -n", "start without connecting"),
        ],
    },
    "stop": {
        "usage": "vst stop [name] [--all]",
        "desc": "Stop a running instance.",
        "examples": [
            ("vst stop", "stop your instance"),
            ("vst stop --all", "stop everything"),
        ],
    },
    "destroy": {
        "usage": "vst destroy [name] [--all]",
        "desc": "Destroy an instance (irreversible).",
        "detail": "Also removes its SSH config and any alias.",
        "examples": [
            ("vst destroy", "destroy your instance"),
            ("vst destroy --all", "destroy everything"),
        ],
    },
    "cp": {
        "usage": "vst cp <up|down> <path> [-i instance]",
        "desc": "Copy files to/from a remote instance.",
        "detail": "Paths are relative to the git repo root.",
        "examples": [
            ("vst cp up .env", "upload a file"),
            ("vst cp down results/", "download a directory"),
        ],
    },
    "name": {
        "usage": "vst name <alias> [-i instance] [--clear]",
        "desc": "Assign a custom name to an instance.",
        "detail": "Aliases work everywhere instance names do.",
        "examples": [
            ("vst name train", "name your instance"),
            ("vst train", "then use it anywhere"),
            ("vst name train --clear", "remove the alias"),
        ],
    },
    "config": {
        "usage": "vst config",
        "desc": "Show current configuration.",
    },
}


def _print_section(header: str, items: list[tuple[str, str]]) -> None:
    """Print a colored section with aligned items."""
    width = max(len(k) for k, _ in items)
    print(cyan(f"{header}:"))
    for key, desc in items:
        print(f"  {green(key.ljust(width))}  {desc}")


def _print_help() -> None:
    """Print top-level help and exit."""
    commands = [
        ("list", "list all instances"),
        ("start", "start a stopped instance"),
        ("stop", "stop an instance"),
        ("destroy", "destroy an instance"),
        ("cp", "copy files to/from an instance"),
        ("name", "name an instance"),
        ("config", "show current configuration"),
    ]
    options = [
        ("-f, --force-setup", "re-run remote setup"),
        ("-n, --no-setup", "skip setup (just open IDE)"),
        ("-v, --verbose", "verbose output"),
        ("-h, --help", "show help"),
        ("--version", "show version"),
    ]
    print(f"\n{cyan('usage:')} vst [name] [-f | -n]\n")
    print("Connect to a Vast.ai instance and open your IDE.")
    print(dim("Run with no arguments to auto-select, or pass a name.\n"))
    _print_section("commands", commands)
    print()
    _print_section("options", options)
    hint = "Run 'vst <command> -h' for details on a specific command."
    print(f"\n{dim(hint)}")
    print(dim("docs: https://github.com/seamus-fallows/vastly") + "\n")
    sys.exit(0)


def _print_cmd_help(cmd: str, parsers: dict) -> None:
    """Print help for a subcommand and exit."""
    info = _CMD_HELP[cmd]
    print(f"\n{cyan('usage:')} {info['usage']}\n")
    print(f"{info['desc']}")
    if info.get("detail"):
        print(dim(info["detail"]))
    print()

    # Derive arguments and options from the argparse parser
    pos_args: list[tuple[str, str]] = []
    opts: list[tuple[str, str]] = []
    for action in parsers[cmd]._actions:
        if action.dest in ("help", "verbose") or not action.help:
            continue
        if action.option_strings:
            opts.append((", ".join(action.option_strings), action.help))
        else:
            name = "|".join(action.choices) if action.choices else action.dest
            pos_args.append((name, action.help))

    if pos_args:
        _print_section("arguments", pos_args)
        print()
    if opts:
        _print_section("options", opts)
        print()
    if info.get("examples"):
        width = max(len(ex) for ex, _ in info["examples"])
        print(cyan("examples:"))
        for ex, desc in info["examples"]:
            print(f"  {green(ex.ljust(width))}  {dim(desc)}")
        print()
    sys.exit(0)


def _build_parser() -> tuple[argparse.ArgumentParser, dict]:
    """Build and return (parser, {cmd_name: subparser})."""
    hint = "auto-selects if only one, prompts if multiple"

    parser = argparse.ArgumentParser(prog="vst", add_help=False)
    parser.add_argument("--version", action="version", version=f"vastly {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    g_top = parser.add_mutually_exclusive_group()
    g_top.add_argument("-n", "--no-setup", action="store_true")
    g_top.add_argument("-f", "--force-setup", action="store_true")

    subparsers = parser.add_subparsers(dest="command")
    parsers: dict[str, argparse.ArgumentParser] = {}

    # Connect (default when no subcommand given)
    p = subparsers.add_parser("connect", help="connect to an instance and open IDE")
    p.add_argument("name", nargs="?", help=f"instance name or alias ({hint})")
    p.add_argument("-v", "--verbose", action="store_true")
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "-n",
        "--no-setup",
        action="store_true",
        help="skip remote setup (just open IDE)",
    )
    g.add_argument(
        "-f",
        "--force-setup",
        action="store_true",
        help="re-run setup even if already done",
    )
    parsers["connect"] = p

    # List
    p = subparsers.add_parser("list", help="list running instances")
    p.add_argument("-v", "--verbose", action="store_true")
    parsers["list"] = p

    # Stop
    p = subparsers.add_parser("stop", help="stop a running instance")
    p.add_argument("name", nargs="?", help=f"instance name or alias ({hint})")
    p.add_argument("--all", action="store_true", help="stop all running instances")
    p.add_argument("-v", "--verbose", action="store_true")
    parsers["stop"] = p

    # Destroy
    p = subparsers.add_parser("destroy", help="destroy an instance (irreversible)")
    p.add_argument("name", nargs="?", help=f"instance name or alias ({hint})")
    p.add_argument("--all", action="store_true", help="destroy all instances")
    p.add_argument("-v", "--verbose", action="store_true")
    parsers["destroy"] = p

    # Cp
    p = subparsers.add_parser("cp", help="copy files to/from a remote instance")
    p.add_argument("direction", choices=["up", "down"], help="upload or download")
    p.add_argument("path", help="file or directory (append / for directories)")
    p.add_argument("-i", "--instance", help=f"target instance ({hint})")
    p.add_argument("-v", "--verbose", action="store_true")
    parsers["cp"] = p

    # Start
    p = subparsers.add_parser("start", help="start a stopped instance")
    p.add_argument("name", nargs="?", help=f"instance name or alias ({hint})")
    p.add_argument(
        "-n", "--no-connect", action="store_true", help="start without connecting"
    )
    p.add_argument("-v", "--verbose", action="store_true")
    parsers["start"] = p

    # Name
    p = subparsers.add_parser("name", help="assign a custom name to an instance")
    p.add_argument("alias", help="name to assign")
    p.add_argument("-i", "--instance", help=f"instance to name ({hint})")
    p.add_argument("--clear", action="store_true", help="remove the alias")
    p.add_argument("-v", "--verbose", action="store_true")
    parsers["name"] = p

    # Config
    p = subparsers.add_parser("config", help="show current configuration")
    p.add_argument("-v", "--verbose", action="store_true")
    parsers["config"] = p

    return parser, parsers


def main() -> None:
    """Parse arguments and dispatch to the appropriate subcommand."""
    parser, parsers = _build_parser()

    # Handle help ourselves for clean, colored output
    if {"-h", "--help"} & set(sys.argv[1:]):
        rest = [a for a in sys.argv[1:] if a not in ("-h", "--help")]
        cmd = rest[0] if rest and not rest[0].startswith("-") else None
        if cmd in _CMD_HELP:
            _print_cmd_help(cmd, parsers)
        else:
            _print_help()

    # Treat unknown subcommands as instance names for connect.
    # e.g. `vst my-gpu` becomes `vst connect my-gpu`
    # e.g. `vst -v my-gpu` becomes `vst -v connect my-gpu`
    known = set(parsers.keys())
    non_flags = [a for a in sys.argv[1:] if not a.startswith("-")]
    if non_flags and non_flags[0] not in known:
        idx = sys.argv.index(non_flags[0])
        sys.argv.insert(idx, "connect")

    args = parser.parse_args()

    # No subcommand = connect with auto-select
    if args.command is None:
        args.command = "connect"
        args.name = None

    # Subparser defaults can override the main parser's flags, so check argv directly
    if "-v" in sys.argv[1:] or "--verbose" in sys.argv[1:]:
        args.verbose = True
    if "-f" in sys.argv[1:] or "--force-setup" in sys.argv[1:]:
        args.force_setup = True
    if "-n" in sys.argv[1:] or "--no-setup" in sys.argv[1:]:
        args.no_setup = True

    if args.verbose:
        vastly.VERBOSE = True

    try:
        if args.command == "connect":
            _cmd_connect(args)
        elif args.command == "list":
            _cmd_list(args)
        elif args.command == "start":
            _cmd_start(args)
        elif args.command == "stop":
            _cmd_stop(args)
        elif args.command == "destroy":
            _cmd_destroy(args)
        elif args.command == "name":
            _cmd_name(args)
        elif args.command == "cp":
            _cmd_cp(args)
        elif args.command == "config":
            _cmd_config(args)
    except KeyboardInterrupt:
        print(file=sys.stderr)  # clean up partial line
        sys.exit(130)  # standard exit code for SIGINT
    except VastlyError as e:
        print(red(str(e)), file=sys.stderr)
        sys.exit(e.exit_code)
