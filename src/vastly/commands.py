"""Subcommand handlers for the vst CLI."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import vastly
from vastly import __version__, cyan, dim, green, red, yellow
from vastly.config import CONFIG_PATH, _PROJECT_KEYS, load_config
from vastly.errors import VastlyError
from vastly.ide import check_ide, open_ide
from vastly.instance import (
    STOPPED_STATES,
    STOPPABLE_STATES,
    TRANSITIONAL_STATES,
    Instance,
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
from vastly.ssh import SSH_CONFIG_DIR, SSH_OPTS


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
    """Verify required tools are available. Raises VastlyError on failure."""
    missing = []

    if not shutil.which("vastai"):
        missing.append("Missing: vastai CLI. Install with: pip install vastai")
    if not shutil.which("git"):
        missing.append("Missing: git. Install from https://git-scm.com")
    if not shutil.which("ssh"):
        missing.append("Missing: ssh.")
    if need_ide and not check_ide(ide):
        other = {"code": "cursor", "cursor": "code"}.get(ide)
        if other and check_ide(other):
            missing.append(f"Missing: {ide}, but {other} is installed.")
            missing.append(f'  Update "ide" in ~/.vastly.json to "{other}" to use it.')
        else:
            urls = {
                "code": "https://code.visualstudio.com",
                "cursor": "https://cursor.com",
            }
            url = urls.get(ide, "")
            hint = f" Download from {url}" if url else ""
            missing.append(f"Missing: {ide}.{hint}")

    if missing:
        raise VastlyError("\n".join(missing))


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


# ── Vastai wrappers ─────────────────────────────────────────────────


def _vastai_action(action: str, inst: Instance) -> None:
    """Run 'vastai stop/destroy instance <id>' and print result."""
    if inst.id is None:
        raise VastlyError(
            f"Cannot {action} cached instance '{inst.name}' (no instance ID)"
        )

    result = subprocess.run(
        ["vastai", action, "instance", str(inst.id)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise VastlyError(f"Failed to {action} {inst.name}: {msg}")

    action_past = {"stop": "Stopped", "destroy": "Destroyed", "start": "Started"}[action]
    print(green(f"  {action_past} {inst.name}"))


def _vastai_destroy(inst: Instance) -> None:
    """Destroy an instance and clean up its SSH config and alias."""
    _vastai_action("destroy", inst)

    # Clean up SSH config for the destroyed instance
    config_file = SSH_CONFIG_DIR / inst.name
    if config_file.exists():
        config_file.unlink()

    # Clean up alias SSH config and alias entry
    inst_id = str(inst.id)
    aliases = load_aliases()
    alias = aliases.pop(inst_id, None)
    if alias:
        save_aliases(aliases)
        alias_config = SSH_CONFIG_DIR / alias
        if alias_config.exists():
            alias_config.unlink()


_START_TIMEOUT = 300  # 5 minutes
_START_POLL_INTERVAL = 5


def _poll_for_running(inst_id: str) -> None:
    """Poll the Vast.ai API until instance is running, or raise on timeout."""

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


# ── Subcommand handlers ────────────────────────────────────────────


def cmd_connect(args: argparse.Namespace) -> None:
    """Connect flow -- sync instances, check setup, run setup if needed, open IDE."""
    git_root = _git_root()
    config = load_config(project_dir=git_root)

    _check_prerequisites(need_ide=True, ide=config["ide"])

    all_instances = sync_instances(config)
    running = [i for i in all_instances if i.status == "running"]

    if not running:
        # Auto-start a stopped instance instead of erroring
        startable = [i for i in all_instances if i.status in STOPPED_STATES]

        if args.name:
            match = [
                i
                for i in startable
                if i.name == args.name or i.alias == args.name
            ]
            if match:
                inst = match[0]
            else:
                match_all = [
                    i
                    for i in all_instances
                    if i.name == args.name or i.alias == args.name
                ]
                if match_all:
                    raise VastlyError(
                        f"'{args.name}' is {match_all[0].status} and cannot be started."
                    )
                raise VastlyError(f"No instance named '{args.name}'.")
        elif not startable:
            raise VastlyError("No Vast instances found.")
        elif len(startable) == 1:
            inst = startable[0]
            print(yellow(f"  No running instances. Starting {inst.display_name}..."))
        else:
            print(yellow("  No running instances. Select one to start:"))
            show_table(startable)
            inst = select_instance(startable)[0]

        _vastai_action("start", inst)
        _poll_for_running(str(inst.id))

        # Re-sync for fresh SSH configs
        all_instances = sync_instances(config)
        running = [i for i in all_instances if i.status == "running"]
        if not running:
            raise VastlyError(
                "Instance started but not reachable. Check Vast.ai dashboard."
            )

    show_table(running)

    # If the user named a non-running instance, give a specific error
    if args.name:
        match_running = [
            i for i in running if i.name == args.name or i.alias == args.name
        ]
        if not match_running:
            match_all = [
                i
                for i in all_instances
                if i.name == args.name or i.alias == args.name
            ]
            if match_all:
                raise VastlyError(
                    f"'{args.name}' is {match_all[0].status}. Use 'vst start {args.name}' to start it."
                )

    selected = select_instance(running, args.name, allow_all=True)
    vastly.verbose(f"Selected {len(selected)} instance(s) for connect")

    repo_info = _local_repo_info(config["gitRemote"])

    if args.no_setup or not repo_info:
        vastly.verbose("Skipping setup (--no-setup or not in a git repo)")
        if not repo_info and not args.no_setup:
            print(
                yellow(
                    "  Not in a git repo. Tip: run vst from inside a git repo to auto-setup."
                )
            )
        for inst in selected:
            print(green(f"  Opening {config['workspace']}"))
            open_ide(config["ide"], inst.name, config["workspace"])
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


def cmd_list(args: argparse.Namespace) -> None:
    """List running instances."""
    git_root = _git_root()
    config = load_config(project_dir=git_root)

    _check_prerequisites(ide=config["ide"])

    instances = get_synced_instances(config)
    show_table(instances)


def cmd_name(args: argparse.Namespace) -> None:
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
    inst_id = str(inst.id)

    # Remove any existing alias for this instance (and its SSH config)
    if inst_id in aliases:
        old_alias = aliases[inst_id]
        old_config = SSH_CONFIG_DIR / old_alias
        if old_config.exists():
            old_config.unlink()
        del aliases[inst_id]

    aliases[inst_id] = args.alias
    save_aliases(aliases)
    print(green(f"  Named {inst.name} as '{args.alias}'"))


def cmd_stop(args: argparse.Namespace) -> None:
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
            if i.name == args.name or i.alias == args.name
        ]
        if match and match[0].status in STOPPED_STATES:
            raise VastlyError(f"Already stopped ({match[0].status}).")

    stoppable = [i for i in instances if i.status in STOPPABLE_STATES]
    vastly.verbose(f"Found {len(stoppable)} stoppable instance(s)")
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


def cmd_destroy(args: argparse.Namespace) -> None:
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
        if not _confirm(f"Destroy {selected[0].display_name}? This is irreversible."):
            return
    else:
        if not _confirm(f"Destroy {len(selected)} instances? This is irreversible."):
            return

    for inst in selected:
        _vastai_destroy(inst)


def _copy_one(
    direction: str,
    raw_path: str,
    inst: Instance,
    remote_base: str,
    git_root: Path,
) -> bool:
    """Copy a single file/directory. Returns True on success."""
    from vastly.ssh import run_scp, run_ssh

    rel_path = raw_path.rstrip("/\\")
    remote_path = f"{remote_base}/{rel_path}"
    local_path = git_root / rel_path

    # Detect if path is a directory (for recursive copy)
    is_dir = raw_path.endswith("/") or raw_path.endswith("\\")
    if direction == "up" and local_path.exists():
        is_dir = is_dir or local_path.is_dir()

    if direction == "down":
        local_path.parent.mkdir(parents=True, exist_ok=True)
        src = f"{inst.name}:{remote_path}"
        if is_dir:
            src = f"{inst.name}:{remote_path}/"
        result = run_scp(src, str(local_path), recursive=is_dir)
        if result.returncode != 0:
            msg = result.stderr.strip() or "unknown error"
            print(yellow(f"  Download failed for {rel_path}: {msg}"))
            return False
        print(green(f"  Downloaded {rel_path}"))
        return True

    # Upload
    if not local_path.exists():
        print(yellow(f"  {rel_path} not found locally, skipping"))
        return False
    parent_rel = str(PurePosixPath(rel_path).parent)
    if parent_rel != ".":
        remote_parent = f"{remote_base}/{parent_rel}"
        run_ssh(inst.name, f"mkdir -p {shlex.quote(remote_parent)}")
    dest = f"{inst.name}:{remote_path}"
    result = run_scp(str(local_path), dest, recursive=is_dir)
    if result.returncode != 0:
        msg = result.stderr.strip() or "unknown error"
        print(yellow(f"  Upload failed for {rel_path}: {msg}"))
        return False
    print(green(f"  Uploaded {rel_path}"))
    return True


def cmd_cp(args: argparse.Namespace) -> None:
    """Copy files to/from a remote instance."""
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

    # Build the list of paths to copy
    paths = list(args.paths) if args.paths else []
    if args.config:
        if args.direction == "down":
            raise VastlyError("--config is only supported for uploads (vst cp up -c).")
        copy_files = config["copyFiles"]
        if not copy_files:
            print(yellow("  No copyFiles configured in .vastly.json"))
        else:
            # Prepend config entries, then any extra CLI paths
            paths = copy_files + [p for p in paths if p not in copy_files]

    if not paths:
        raise VastlyError(
            "No paths specified. Usage: vst cp up <path...> or vst cp up -c"
        )

    instances = get_running_instances(config)
    inst = select_instance(instances, args.instance)[0]

    remote_base = f"{config['workspace']}/{repo_name}"

    ok = 0
    for p in paths:
        if _copy_one(args.direction, p, inst, remote_base, git_root):
            ok += 1

    if ok == 0:
        raise VastlyError("All copies failed.")


def cmd_start(args: argparse.Namespace) -> None:
    """Start a stopped/exited instance, wait for readiness, then connect."""
    git_root = _git_root()
    config = load_config(project_dir=git_root)

    _check_prerequisites(ide=config["ide"])

    instances = get_synced_instances(config)

    # Filter to non-running instances for selection
    non_running = [i for i in instances if i.status != "running"]
    if not non_running:
        raise VastlyError(
            "All instances are already running. Run 'vst' to connect."
        )

    inst = select_instance(non_running, args.name)[0]
    vastly.verbose(f"Starting instance {inst.display_name} (status: {inst.status})")

    if inst.status in STOPPED_STATES:
        _vastai_action("start", inst)
    elif inst.status in TRANSITIONAL_STATES:
        print(dim(f"  Instance is {inst.status}, waiting for it to be ready..."))
    else:
        raise VastlyError(f"Cannot start instance in '{inst.status}' state.")

    if args.no_connect:
        return

    _poll_for_running(str(inst.id))

    # Auto-connect: triggers a fresh sync_instances which writes SSH configs
    connect_args = argparse.Namespace(
        command="connect",
        name=inst.alias or inst.name,
        no_setup=False,
        force_setup=False,
        verbose=getattr(args, "verbose", False),
    )
    cmd_connect(connect_args)


def cmd_config(args: argparse.Namespace) -> None:
    """Show resolved configuration."""

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


def cmd_ssh(args: argparse.Namespace) -> None:
    """SSH into a running instance, optionally running a command."""

    if not shutil.which("ssh"):
        raise VastlyError("Missing: ssh.")

    git_root = _git_root()
    config = load_config(project_dir=git_root)

    if not shutil.which("vastai"):
        raise VastlyError("Missing: vastai CLI. Install with: pip install vastai")

    instances = get_running_instances(config)

    # Smart dispatch: if args.name doesn't match any instance, treat it as a
    # remote command.  e.g. `vst ssh nvidia-smi` runs nvidia-smi on the
    # auto-selected instance (same pattern as `vst train` -> `vst connect train`).
    remote_cmd = list(args.remote_cmd) if args.remote_cmd else []
    if args.name:
        match = [i for i in instances if i.name == args.name or i.alias == args.name]
        if match:
            inst = match[0]
        else:
            remote_cmd = [args.name] + remote_cmd
            inst = select_instance(instances)[0]
    else:
        inst = select_instance(instances)[0]

    ssh_cmd = ["ssh", *SSH_OPTS, inst.name]
    if remote_cmd:
        ssh_cmd.extend(remote_cmd)

    vastly.verbose(f"ssh command: {' '.join(ssh_cmd)}")

    # On Unix, replace the process entirely so Ctrl+C, terminal resizing, etc.
    # work natively. On Windows, subprocess is the only option.
    if sys.platform == "win32":
        result = subprocess.run(ssh_cmd)
        sys.exit(result.returncode)
    else:
        os.execvp("ssh", ssh_cmd)
