"""Remote project setup -- upload the setup script and run it."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import tarfile
import time
from importlib import resources
from pathlib import Path

import vastly
from vastly import __version__, cyan, dim, gitauth, green, red, yellow
from vastly.config import Config
from vastly.instance import Instance
from vastly.ssh import (
    KNOWN_HOSTS,
    SSH_SETUP_OPTS,
    host_key_alias,
    host_key_changed,
    run_ssh,
    set_forward_agent,
    ssh_program,
)

# These paths must match setup-remote.sh -- keep in sync
REMOTE_MARKER_DIR = "~/.vastly/setup"
REMOTE_MARKER_PATTERN = "~/.vastly/setup/{repo_name}.json"

# Separator used in combined SSH probe commands (read marker + list markers).
# Also referenced in tests -- import from here to avoid duplication.
_PROBE_SEP = "__VASTLY_SEP__"

# Where the setup script and copyFiles are unpacked on the instance
_UPLOAD_DIR = "/tmp/vastly-setup"


def _check_repo_mismatch(repo_name: str, setup_files: list[str]) -> list[str]:
    """Return names of other repos set up on the instance.

    Returns an empty list when no mismatch is detected (safe to proceed).
    """
    return [
        f.removesuffix(".json")
        for f in setup_files
        if f.endswith(".json") and f.removesuffix(".json") != repo_name
    ]


def _for_instance(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Upload files as the SSH user's own, with plain modes (Windows has no Unix ones)."""
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
    return info


def _upload(host: str, script: Path, files: list[tuple[Path, str]]) -> bool:
    """Send the setup script and copyFiles to _UPLOAD_DIR in one SSH connection.

    *files* are (local path, path in the repo) pairs. Everything goes as one tar
    stream, so large copyFiles entries aren't held in memory.
    """
    remote = (
        f"rm -rf {_UPLOAD_DIR} && mkdir -p {_UPLOAD_DIR} && tar -xf - -C {_UPLOAD_DIR}"
    )
    vastly.verbose(f"ssh {host}: {remote}")
    proc = subprocess.Popen(
        [ssh_program("ssh"), *SSH_SETUP_OPTS, host, remote],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
    )
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|", dereference=True) as tar:
            # Unix line endings, even if git checked the script out with CRLF
            data = script.read_bytes().replace(b"\r\n", b"\n")
            info = tarfile.TarInfo("setup-remote.sh")
            info.size, info.mtime = len(data), time.time()
            tar.addfile(_for_instance(info), io.BytesIO(data))
            for local, rel in files:
                tar.add(local, arcname=f"files/{rel}", filter=_for_instance)
    except OSError as e:
        if e.filename:  # a copyFiles entry couldn't be read
            print(red(f"  Can't read {e.filename}: {e.strerror}"))
        # Otherwise ssh gave up early, and its error is already on screen
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
    try:
        return proc.wait(timeout=60) == 0
    except subprocess.TimeoutExpired:
        proc.kill()
        return False


def _copy_files(
    entries: list[str], project_dir: Path | None, label: str
) -> list[tuple[Path, str]]:
    """The copyFiles entries to upload, as (local path, path in the repo) pairs."""
    if not project_dir:
        return []
    files = []
    for entry in entries:
        rel = entry.replace("\\", "/").rstrip("/")
        local = project_dir / rel
        if not Path(os.path.normpath(local)).is_relative_to(project_dir):
            print(yellow(f"  {label}: copyFiles: {rel} is outside the repo, skipping"))
        elif not local.exists():
            print(yellow(f"  {label}: copyFiles: {rel} not found locally, skipping"))
        else:
            files.append((local, rel))
    return files


def _add_deploy_key(
    inst: Instance, repo: str, repo_name: str, account: int | None
) -> str | None:
    """Create a deploy key on *inst* and register it on *repo*.

    Returns None on success, or a short reason it didn't work.
    """
    title = gitauth.key_title(inst.id, account)
    try:
        public_key = gitauth.ensure_instance_key(inst.name, repo_name, title)
    except gitauth.GitHubError as e:
        return str(e)
    try:
        key_id = gitauth.add_deploy_key(repo, title, public_key)
    except gitauth.GitHubError as e:
        return f"GitHub rejected the key ({e})"
    gitauth.record_deploy_key(inst.id, repo, key_id, account)
    return None


def setup_instances(
    instances: list[Instance],
    repo_url: str,
    repo_name: str,
    config: Config,
    *,
    force_setup: bool = False,
    project_dir: Path | None = None,
    live_ids: set[int] | None = None,
) -> list[str]:
    """Run remote setup on each instance. Returns list of successful host names.

    *live_ids* are all instance IDs on the current Vast.ai account; when given,
    the repo's stale vastly deploy keys are cleaned up while setting up keys.
    """
    git_name = None
    git_email = None

    setup_script = Path(str(resources.files("vastly.data").joinpath("setup-remote.sh")))
    if not setup_script.exists():
        print(
            red(
                "Setup script not found. If installed from a zip, try: pip install vastly"
            )
        )
        return []

    install_cmd = config["installCommand"] or "auto"
    disable_tmux = "true" if config["disableAutoTmux"] else "false"
    success_names = []

    quoted_name = shlex.quote(repo_name)
    https_warned = False

    # Git access (see gitauth.py). Whether a deploy key is possible for this
    # repo is checked once, and only if some instance actually needs setup.
    mode = config["gitAuth"]
    github = gitauth.github_repo(repo_url)
    blocker: str | None = None
    blocker_checked = False
    account: int | None = None
    pruned = False

    for inst in instances:
        name = inst.name
        label = inst.display_name
        print(f"  {label}: ", end="", flush=True)

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
            if host_key_changed(marker.stderr):
                break
            if attempt < 3:
                print(yellow(f" retry {attempt + 1}/3..."), end="", flush=True)
                time.sleep(5)

        if not reachable and host_key_changed(marker.stderr):
            print(red(" its SSH host key has changed, so vastly won't connect."))
            print(
                red(
                    "  If you recycled or rebuilt the instance, remove the old key and try again:\n"
                    f'    ssh-keygen -R {host_key_alias(inst.id)} -f "{KNOWN_HOSTS}"'
                )
            )
            continue
        if not reachable:
            print(
                red(
                    " unreachable. Check that the instance is running and your SSH key is loaded (ssh-add -l)."
                )
            )
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
                print(f"  {label}: skipped.")
                continue

        # Fetch git identity lazily -- only when setup is actually needed.
        # No --global: use the identity that applies to this repo (repo-local
        # config and includeIf rules win over the global one).
        if git_name is None:
            git_name = subprocess.run(
                ["git", "config", "user.name"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=project_dir,
            ).stdout.strip()
            git_email = subprocess.run(
                ["git", "config", "user.email"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=project_dir,
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

        # Per-repo deploy key where possible; otherwise SSH agent forwarding
        use_key = False
        if mode != "agent":
            if not blocker_checked:
                blocker_checked = True
                blocker = gitauth.deploy_key_blocker(github)
                if blocker is None:
                    account = gitauth.account_id()
            problem = blocker or _add_deploy_key(inst, github, repo_name, account)
            if problem is None:
                use_key = True
                print(dim(f"  {label}: using a deploy key for {github}"))
                # Once per run: remove keys left by destroyed instances (also
                # ones created from other machines)
                if not pruned and live_ids is not None and account is not None:
                    pruned = True
                    gitauth.prune_repo_keys(github, account, live_ids)
            elif mode == "deploy-key":
                print(red(f"  {label}: can't use a deploy key -- {problem}."))
                print(
                    red(
                        "  Fix that, or run 'vst --git-auth agent' to forward "
                        "your SSH agent instead."
                    )
                )
                continue
            else:
                print(yellow(f"  {label}: using SSH agent forwarding -- {problem}."))

        # Remember when this repo relies on forwarding, so a later deploy-key mode
        # doesn't switch it off. Pure agent-mode users never get a state entry.
        if not use_key and (mode != "agent" or gitauth.is_tracked(inst.id)):
            gitauth.record_agent(inst.id, account)

        # Warn about HTTPS limitation (once). Deploy keys always use SSH.
        if not use_key and not https_warned and repo_url.startswith("https://"):
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

        files = _copy_files(config["copyFiles"], project_dir, label)
        if not _upload(name, setup_script, files):
            print(red(f"  {label}: failed to upload the setup files"))
            continue

        setup_args = [
            gitauth.ssh_url(github) if use_key else repo_url,
            repo_name,
            git_name,
            git_email,
            config["workspace"],
            disable_tmux,
            install_cmd,
            __version__,
            "deploy-key" if use_key else "agent",
        ] + config["postInstall"]

        quoted = " ".join(shlex.quote(a) for a in setup_args)
        # copyFiles go into the repo once setup has cloned it
        repo_dir = shlex.quote(f"{config['workspace']}/{repo_name}")
        remote_cmd = (
            f"bash {_UPLOAD_DIR}/setup-remote.sh {quoted}; e=$?; "
            f"if [ $e -eq 0 ] && [ -d {_UPLOAD_DIR}/files ]; then "
            f"echo ':: Copying copyFiles into '{repo_dir}; "
            f"cp -a {_UPLOAD_DIR}/files/. {repo_dir}/ "
            "|| echo ':: [WARN] Copying copyFiles failed' >&2; fi; "
            f"rm -rf {_UPLOAD_DIR}; exit $e"
        )

        result = run_ssh(name, remote_cmd, setup=True, stream=True)

        if result.returncode != 0:
            print(red(f"  {label}: setup failed (exit {result.returncode})"))
            continue

        # Agent forwarding is only needed if some repo on this instance uses it
        if mode != "agent":
            forward = gitauth.forward_agent(inst.id, mode)
            for host in (inst.name, inst.alias):
                if host:
                    set_forward_agent(host, forward)

        print(green(f"  {label}: done."))
        success_names.append(name)

    # Summary line for partial success
    total = len(instances)
    ok = len(success_names)
    if total > 1 and ok < total:
        print(yellow(f"  {ok}/{total} instances set up successfully."))

    return success_names
