"""Remote project setup -- SCP setup script and execute."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from importlib import resources
from pathlib import Path, PurePosixPath
from vastly import __version__, cyan, green, red, yellow
from vastly.config import Config
from vastly.instance import Instance
from vastly.ssh import run_scp, run_ssh

# These paths must match setup-remote.sh -- keep in sync
REMOTE_MARKER_DIR = "~/.vastly/setup"
REMOTE_MARKER_PATTERN = "~/.vastly/setup/{repo_name}.json"

# Separator used in combined SSH probe commands (read marker + list markers).
# Also referenced in tests -- import from here to avoid duplication.
_PROBE_SEP = "__VASTLY_SEP__"


def _check_repo_mismatch(repo_name: str, setup_files: list[str]) -> list[str]:
    """Return names of other repos set up on the instance.

    Returns an empty list when no mismatch is detected (safe to proceed).
    """
    return [
        f.removesuffix(".json")
        for f in setup_files
        if f.endswith(".json") and f.removesuffix(".json") != repo_name
    ]


def setup_instances(
    instances: list[Instance],
    repo_url: str,
    repo_name: str,
    config: Config,
    *,
    force_setup: bool = False,
    project_dir: Path | None = None,
) -> list[str]:
    """Run remote setup on each instance. Returns list of successful host names."""
    git_name = None
    git_email = None

    setup_script = Path(str(resources.files("vastly.data").joinpath("setup-remote.sh")))
    if not setup_script.exists():
        print(red("Setup script not found. If installed from a zip, try: pip install vastly"))
        return []

    install_cmd = config["installCommand"] or "auto"
    disable_tmux = "true" if config["disableAutoTmux"] else "false"
    success_names = []

    quoted_name = shlex.quote(repo_name)
    https_warned = False

    for inst in instances:
        name = inst.name
        print(f"  {name}: ", end="", flush=True)

        # Combined reachability + marker + listing probe in a single SSH connection.
        # Reads the current repo's marker and lists all markers for mismatch detection.
        # "|| true" ensures exit 0 whenever SSH connects (even if file/dir is missing).
        if force_setup:
            marker_cmd = (
                f"rm -f {REMOTE_MARKER_DIR}/{quoted_name}.json; "
                f"printf '\\n{_PROBE_SEP}\\n'; "
                f"ls {REMOTE_MARKER_DIR}/ 2>/dev/null || true"
            )
        else:
            marker_cmd = (
                f"cat {REMOTE_MARKER_DIR}/{quoted_name}.json 2>/dev/null || true; "
                f"printf '\\n{_PROBE_SEP}\\n'; "
                f"ls {REMOTE_MARKER_DIR}/ 2>/dev/null || true"
            )

        reachable = False
        print("connecting...", end="", flush=True)
        for attempt in range(1, 4):
            marker = run_ssh(name, marker_cmd)
            if marker.returncode == 0:
                reachable = True
                break
            if attempt < 3:
                print(yellow(f" retry {attempt + 1}/3..."), end="", flush=True)
                time.sleep(5)

        if not reachable:
            print(red(" unreachable. Check that the instance is running and your SSH key is loaded (ssh-add -l)."))
            continue

        # Parse probe output: marker JSON (before separator) and setup listing (after)
        parts = marker.stdout.split(_PROBE_SEP)
        marker_json = parts[0].strip()
        listing_str = parts[1].strip() if len(parts) > 1 else ""
        setup_files = listing_str.split("\n") if listing_str else []

        # Marker exists and is valid JSON -- repo is already set up
        if not force_setup and marker_json:
            try:
                marker_data = json.loads(marker_json)
            except json.JSONDecodeError:
                marker_data = None
            if marker_data:
                print(green("already set up."))
                success_names.append(name)
                continue

        # Setup is needed -- check for repo mismatch (other repos on this instance)
        other_repos = _check_repo_mismatch(repo_name, setup_files)
        if other_repos:
            names_str = ", ".join(f"'{r}'" for r in other_repos)
            print()
            print(
                yellow(
                    f"  Warning: this instance has {names_str} set up, "
                    f"but you're in '{repo_name}'."
                )
            )
            try:
                answer = (
                    input(f"  Continue with setup for '{repo_name}'? [y/N] ")
                    .strip()
                    .lower()
                )
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer != "y":
                print(f"  {name}: skipped.")
                continue

        # Warn about HTTPS limitation (once)
        if not https_warned and repo_url.startswith("https://"):
            https_warned = True
            clean_url = repo_url.rstrip("/")
            suggestion = clean_url.replace("https://", "git@", 1).replace("/", ":", 1)
            fix_url = suggestion if suggestion.endswith(".git") else suggestion + ".git"
            print(
                yellow(
                    f"\n  Note: HTTPS remote -- pushing won't work from the instance\n"
                    f"  (credentials can't be forwarded). To fix:\n"
                    f"    git remote set-url {config['gitRemote']} {fix_url}\n"
                )
            )

        # Fetch git identity lazily -- only when setup is actually needed
        if git_name is None:
            git_name = subprocess.run(
                ["git", "config", "--global", "user.name"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            git_email = subprocess.run(
                ["git", "config", "--global", "user.email"],
                capture_output=True,
                text=True,
            ).stdout.strip()

        # Setup is needed -- git identity required
        if not git_name or not git_email:
            print(red("setup needed but git identity not configured."))
            if not git_name:
                print(red('  Run: git config --global user.name "Your Name"'))
            if not git_email:
                print(red('  Run: git config --global user.email "you@example.com"'))
            continue

        print(cyan("running setup..."))

        scp_result = run_scp(
            str(setup_script), f"{name}:/tmp/_vastly-setup.sh", setup=True
        )
        if scp_result.returncode != 0:
            print(red(f"  {name}: failed to copy setup script"))
            continue

        setup_args = [
            repo_url,
            repo_name,
            git_name,
            git_email,
            config["workspace"],
            disable_tmux,
            install_cmd,
            __version__,
        ] + config["postInstall"]

        quoted = " ".join(shlex.quote(a) for a in setup_args)
        remote_cmd = (
            # SCP from Windows preserves CRLF; strip carriage returns for bash
            "sed -i 's/\\r$//' /tmp/_vastly-setup.sh && "
            f"bash /tmp/_vastly-setup.sh {quoted}; "
            "e=$?; rm -f /tmp/_vastly-setup.sh; exit $e"
        )

        result = run_ssh(name, remote_cmd, setup=True, stream=True)

        if result.returncode != 0:
            print(red(f"  {name}: setup failed (exit {result.returncode})"))
            continue

        # Copy non-git-tracked files to the remote instance
        copy_files = config["copyFiles"]
        if copy_files and project_dir:
            remote_base = f"{config['workspace']}/{repo_name}"
            for rel_path in copy_files:
                local_path = project_dir / rel_path
                if not local_path.exists():
                    print(
                        yellow(
                            f"  {name}: copyFiles: {rel_path} not found locally, skipping"
                        )
                    )
                    continue
                remote_dest = f"{name}:{remote_base}/{rel_path}"
                print(cyan(f"  {name}: copying {rel_path}"))
                # Ensure parent directory exists on remote (use PurePosixPath
                # so we get forward slashes even when running on Windows)
                parent_rel = str(PurePosixPath(rel_path).parent)
                if parent_rel != ".":
                    remote_parent = f"{remote_base}/{parent_rel}"
                    run_ssh(name, f"mkdir -p {shlex.quote(remote_parent)}")
                cp = run_scp(
                    str(local_path),
                    remote_dest,
                    setup=True,
                    recursive=local_path.is_dir(),
                )
                if cp.returncode != 0:
                    print(yellow(f"  {name}: failed to copy {rel_path}"))

        print(green(f"  {name}: done."))
        success_names.append(name)

    # Summary line for partial success
    total = len(instances)
    ok = len(success_names)
    if total > 1 and ok < total:
        print(yellow(f"  {ok}/{total} instances set up successfully."))

    return success_names
