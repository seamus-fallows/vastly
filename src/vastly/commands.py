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
from difflib import get_close_matches
from pathlib import Path, PurePosixPath

import vastly
from vastly import __version__, cyan, dim, green, red, yellow
from vastly.config import CONFIG_PATH, Config, _PROJECT_KEYS, load_config
from vastly.errors import VastlyError
from vastly.ide import check_ide, open_ide
from vastly.instance import (
    STARTABLE_STATES,
    STOPPED_STATES,
    STOPPABLE_STATES,
    TRANSITIONAL_STATES,
    Instance,
    find_by_name,
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


def _check_prerequisites(*, need_ide: bool = False, ide: str = "") -> str:
    """Verify required tools are available. Returns the resolved IDE name.

    When *need_ide* is True and the configured IDE isn't found, falls back
    to the other IDE if installed. Raises VastlyError on hard failures.
    """
    missing = []

    if not shutil.which("vastai"):
        missing.append("Missing: vastai CLI. Install with: pip install vastai")
    if not shutil.which("git"):
        missing.append("Missing: git.")
    if not shutil.which("ssh"):
        missing.append("Missing: ssh.")
    if need_ide and not check_ide(ide):
        other = {"code": "cursor", "cursor": "code"}.get(ide)
        if other and check_ide(other):
            print(dim(f"  Using {other} ({ide} not found)"))
            ide = other
        else:
            missing.append(f"Missing: {ide}.")

    if missing:
        raise VastlyError("\n".join(missing))

    return ide


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
    result = subprocess.run(
        ["vastai", action, "instance", str(inst.id)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise VastlyError(f"Failed to {action} {inst.display_name}: {msg}")

    action_past = {"stop": "Stopped", "destroy": "Destroyed"}[action]
    print(green(f"  {action_past} {inst.display_name}"))


def _vastai_start(inst: Instance) -> bool:
    """Start an instance. Returns True if the start was queued (resources unavailable)."""
    result = subprocess.run(
        ["vastai", "start", "instance", str(inst.id)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise VastlyError(f"Failed to start {inst.display_name}: {msg}")

    output = (result.stdout + result.stderr).lower()
    queued = "queued" in output or "unavailable" in output
    if queued:
        print(yellow(f"  Queued {inst.display_name} (waiting for resources)"))
    else:
        print(green(f"  Started {inst.display_name}"))
    return queued


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
_MAX_POLL_FAILURES = 5


def _start_and_resync(
    to_start: list[Instance], config: Config
) -> tuple[list[Instance], list[Instance]]:
    """Start or wait for instances, then re-sync SSH configs.

    Stopped/exited instances are started. Transitional instances (loading,
    creating, etc.) are waited on without issuing a redundant start call.

    Returns (all_instances, running_instances) after re-sync.
    Raises VastlyError if no instances are running after re-sync.
    """
    queued_ids: set[int] = set()
    for inst in to_start:
        if inst.status in TRANSITIONAL_STATES:
            print(dim(f"  {inst.display_name} is already starting, waiting..."))
        else:
            if _vastai_start(inst):
                queued_ids.add(inst.id)
    for inst in to_start:
        _poll_for_running(
            str(inst.id), inst.display_name, queued=inst.id in queued_ids
        )

    all_instances = sync_instances(config)
    running = [i for i in all_instances if i.status == "running"]
    if not running:
        raise VastlyError(
            "Instance started but not reachable -- it may still be initializing. Check the Vast.ai dashboard."
        )
    return all_instances, running


def _poll_for_running(
    inst_id: str, display_name: str = "", *, queued: bool = False
) -> None:
    """Poll the Vast.ai API until instance is running, or raise on timeout.

    When *queued* is True (the start response said resources are unavailable),
    the timeout is suspended -- queued starts can take hours and the user can
    Ctrl+C. The timeout resumes once the instance leaves the stopped state.
    """
    label = display_name or inst_id
    deadline = time.monotonic() + _START_TIMEOUT
    last_status = "unknown"
    api_failures = 0

    while queued or time.monotonic() < deadline:
        time.sleep(_START_POLL_INTERVAL)

        result = subprocess.run(
            ["vastai", "show", "instance", inst_id, "--raw"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            api_failures += 1
            if api_failures >= _MAX_POLL_FAILURES:
                msg = result.stderr.strip() or result.stdout.strip() or "unknown error"
                raise VastlyError(
                    f"Cannot reach Vast.ai API while waiting for {label}: {msg}"
                )
            continue

        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            api_failures += 1
            if api_failures >= _MAX_POLL_FAILURES:
                raise VastlyError(
                    f"Vast.ai API returning invalid data while waiting for {label}."
                )
            continue

        api_failures = 0

        cur_state = data.get("cur_state", "unknown")
        if cur_state == "running":
            actual = data.get("actual_status")
            if actual is None or actual == "running":
                print(green(f"  {label} is running."))
                return
            # cur_state says running but container isn't ready yet
            cur_state = actual

        # Once a queued instance leaves "stopped", it's actively starting --
        # resume the normal timeout from this point.
        if queued and cur_state not in STOPPED_STATES:
            queued = False
            deadline = time.monotonic() + _START_TIMEOUT

        if cur_state != last_status:
            last_status = cur_state
            hint = (
                " (Ctrl+C to cancel -- this can take a while)"
                if queued
                else ""
            )
            print(dim(f"  {label}: waiting...{hint}"))

    raise VastlyError(
        f"Timed out waiting for {label} to start. "
        "Check the Vast.ai dashboard for status."
    )


# ── Subcommand handlers ────────────────────────────────────────────


def _do_connect(
    *,
    name: str | None = None,
    select_all: bool = False,
    no_setup: bool = False,
    force_setup: bool = False,
) -> None:
    """Core connect flow: sync instances, run setup, open IDE."""
    git_root = _git_root()
    config = load_config(project_dir=git_root)

    config["ide"] = _check_prerequisites(need_ide=True, ide=config["ide"])

    all_instances = sync_instances(config)
    running = [i for i in all_instances if i.status == "running"]

    if not running:
        # Include both stopped and transitional (loading, creating, etc.)
        startable = [
            i for i in all_instances
            if i.status in STARTABLE_STATES
        ]

        if name:
            match = find_by_name(startable, name)
            if match:
                to_start = [match]
            else:
                if find_by_name(all_instances, name):
                    raise VastlyError(
                        f"'{name}' is inactive and cannot be started."
                    )
                raise VastlyError(f"No instance named '{name}'.")
        elif not startable:
            raise VastlyError("No Vast instances found.")
        elif select_all:
            print(yellow(f"  No running instances. Starting all {len(startable)}..."))
            to_start = startable
        elif len(startable) == 1:
            to_start = [startable[0]]
            print(
                yellow(
                    f"  No running instances. Starting {to_start[0].display_name}..."
                )
            )
        else:
            print(yellow("  No running instances. Select one to start:"))
            show_table(startable)
            to_start = [select_instance(startable)[0]]

        all_instances, running = _start_and_resync(to_start, config)

    show_table(running)

    # If the user named a non-running instance, give a specific error
    if name:
        if not find_by_name(running, name):
            if find_by_name(all_instances, name):
                raise VastlyError(
                    f"'{name}' is inactive. Use 'vst start {name}' to start it."
                )

    if select_all:
        selected = running
    else:
        selected = select_instance(running, name, allow_all=True)
    vastly.verbose(f"Selected {len(selected)} instance(s) for connect")

    repo_info = _local_repo_info(config["gitRemote"])

    if no_setup or not repo_info:
        vastly.verbose("Skipping setup (--no-setup or not in a git repo)")
        if repo_info:
            _, repo_name = repo_info
            open_path = f"{config['workspace']}/{repo_name}"
        else:
            open_path = config["workspace"]
            if not no_setup:
                print(
                    yellow(
                        "  Not in a git repo -- run vst from a git repo to auto-setup."
                    )
                )
        for inst in selected:
            print(green(f"  Opening {open_path} on {inst.display_name}"))
            open_ide(config["ide"], inst.name, open_path)
        return

    repo_url, repo_name = repo_info

    remote_path = f"{config['workspace']}/{repo_name}"

    success_names = setup_instances(
        selected,
        repo_url,
        repo_name,
        config,
        force_setup=force_setup,
        project_dir=git_root,
    )
    display_names = {inst.name: inst.display_name for inst in selected}
    for inst_name in success_names:
        print(green(f"  Opening {remote_path} on {display_names.get(inst_name, inst_name)}"))
        open_ide(config["ide"], inst_name, remote_path)

    # Only check for updates after successful connect
    from vastly.update import check_for_update

    check_for_update()


def cmd_connect(args: argparse.Namespace) -> None:
    """Connect flow -- sync instances, check setup, run setup if needed, open IDE."""
    _do_connect(
        name=args.name,
        select_all=args.all,
        no_setup=args.no_setup,
        force_setup=args.force_setup,
    )


def cmd_list(args: argparse.Namespace) -> None:
    """List running instances."""
    git_root = _git_root()
    config = load_config(project_dir=git_root)

    _check_prerequisites()

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

    _check_prerequisites()

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

    _check_prerequisites()

    instances = get_synced_instances(config)

    # Give a specific error if named instance exists but can't be stopped
    if args.name:
        match = find_by_name(instances, args.name)
        if match and match.status not in STOPPABLE_STATES:
            raise VastlyError(f"{match.display_name} is already inactive.")

    stoppable = [i for i in instances if i.status in STOPPABLE_STATES]
    vastly.verbose(f"Found {len(stoppable)} stoppable instance(s)")
    if not stoppable:
        raise VastlyError("No running instances to stop.")

    if args.all:
        selected = stoppable
    else:
        selected = select_instance(stoppable, args.name, allow_all=True)

    if len(selected) > 1 and not args.yes:
        if not _confirm(f"Stop {len(selected)} instances?"):
            return

    for inst in selected:
        _vastai_action("stop", inst)


def cmd_destroy(args: argparse.Namespace) -> None:
    """Destroy one or more instances (irreversible)."""
    git_root = _git_root()
    config = load_config(project_dir=git_root)

    _check_prerequisites()

    instances = get_synced_instances(config)

    if args.all:
        selected = instances
    else:
        selected = select_instance(instances, args.name, allow_all=True)

    if not args.yes:
        if len(selected) == 1:
            if not _confirm(
                f"Destroy {selected[0].display_name}? This is irreversible."
            ):
                return
        else:
            if not _confirm(
                f"Destroy {len(selected)} instances? This is irreversible."
            ):
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

    _check_prerequisites()

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

    copied = 0
    for p in paths:
        if _copy_one(args.direction, p, inst, remote_base, git_root):
            copied += 1

    if copied == 0:
        raise VastlyError("No files were copied (see errors above).")


def cmd_start(args: argparse.Namespace) -> None:
    """Start a stopped/exited instance, wait for readiness, then connect."""
    git_root = _git_root()
    config = load_config(project_dir=git_root)

    _check_prerequisites()

    instances = get_synced_instances(config)

    # Give a specific error if the named instance is already running
    if args.name:
        match = find_by_name(instances, args.name)
        if match and match.status == "running":
            raise VastlyError(
                f"'{args.name}' is already running. Run 'vst {args.name}' to connect."
            )

    # Filter to non-running instances for selection
    non_running = [i for i in instances if i.status != "running"]
    if not non_running:
        raise VastlyError("All instances are already running. Run 'vst' to connect.")

    inst = select_instance(non_running, args.name)[0]
    vastly.verbose(f"Starting instance {inst.display_name} (status: {inst.status})")

    queued = False
    if inst.status in STOPPED_STATES:
        queued = _vastai_start(inst)
    elif inst.status in TRANSITIONAL_STATES:
        print(dim(f"  {inst.display_name} is already starting, waiting..."))
    else:
        raise VastlyError(f"Cannot start {inst.display_name} -- it is inactive and not startable.")

    if args.no_connect:
        return

    _poll_for_running(str(inst.id), inst.display_name, queued=queued)

    # Auto-connect: triggers a fresh sync_instances which writes SSH configs
    _do_connect(name=inst.alias or inst.name)


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
            return {"sshKeyPath": "(default)", "installCommand": "(auto)"}.get(
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

    all_instances = sync_instances(config)
    running = [i for i in all_instances if i.status == "running"]

    if not running:
        startable = [
            i for i in all_instances
            if i.status in STARTABLE_STATES
        ]
        if not startable:
            raise VastlyError("No Vast instances found.")

        if args.name:
            match = find_by_name(startable, args.name)
            to_start = [match] if match else None
        else:
            to_start = None

        if to_start is None:
            if len(startable) == 1:
                to_start = [startable[0]]
                print(
                    yellow(
                        f"  No running instances. Starting {to_start[0].display_name}..."
                    )
                )
            else:
                print(yellow("  No running instances. Select one to start:"))
                show_table(startable)
                to_start = [select_instance(startable)[0]]

        all_instances, running = _start_and_resync(to_start, config)

    # Smart dispatch: if args.name doesn't match any running instance, check
    # stopped instances (for a helpful error) before falling back to treating
    # it as a remote command.
    remote_cmd = list(args.remote_cmd) if args.remote_cmd else []
    if args.name:
        match = find_by_name(running, args.name)
        if match:
            selected = [match]
        else:
            # Check if name matches a stopped/transitional instance -- auto-start it
            found_all = find_by_name(all_instances, args.name)
            if found_all and found_all.status in STARTABLE_STATES:
                print(
                    yellow(
                        f"  '{args.name}' is inactive. Starting {found_all.display_name}..."
                    )
                )
                all_instances, running = _start_and_resync([found_all], config)
                match = find_by_name(running, args.name)
                if not match:
                    raise VastlyError("Instance started but not reachable -- it may still be initializing. Check the Vast.ai dashboard.")
                selected = [match]
            elif found_all:
                # Exists but in a non-startable state
                raise VastlyError(
                    f"{found_all.display_name} is {found_all.status} and cannot be started."
                )
            else:
                # No match at all -- check for typos before treating as command
                all_names = []
                for i in all_instances:
                    all_names.append(i.name)
                    if i.alias:
                        all_names.append(i.alias)
                close = get_close_matches(args.name, all_names, n=1, cutoff=0.5)
                if close:
                    raise VastlyError(
                        f"No instance named '{args.name}'. Did you mean '{close[0]}'?"
                    )
                # Treat as remote command
                remote_cmd = [args.name] + remote_cmd
                selected = select_instance(running, allow_all=True)
    else:
        selected = select_instance(running, allow_all=True)

    # Multiple instances only makes sense with a remote command
    if len(selected) > 1 and not remote_cmd:
        raise VastlyError(
            "Cannot open interactive SSH to multiple instances. Specify a command or pick one."
        )

    sys.stdout.flush()

    if len(selected) == 1 and not remote_cmd:
        # Interactive SSH -- replace the process for native terminal behavior
        ssh_cmd = ["ssh", *SSH_OPTS, selected[0].name]
        vastly.verbose(f"ssh command: {' '.join(ssh_cmd)}")
        if sys.platform == "win32":
            result = subprocess.run(ssh_cmd)
            sys.exit(result.returncode)
        else:
            os.execvp("ssh", ssh_cmd)
    else:
        # Run command on one or more instances
        failed = 0
        for inst in selected:
            ssh_cmd = ["ssh", *SSH_OPTS, inst.name, *remote_cmd]
            vastly.verbose(f"ssh command: {' '.join(ssh_cmd)}")
            if len(selected) > 1:
                print(green(f"  ── {inst.display_name} ──"))
            result = subprocess.run(ssh_cmd)
            if result.returncode != 0:
                failed += 1
        if failed:
            sys.exit(1)
