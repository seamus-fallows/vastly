"""Git access on instances: SSH agent forwarding or per-repo GitHub deploy keys.

Agent forwarding lets the instance use your local SSH key while you're
connected. A deploy key is a key pair generated on the instance and registered
(via the GitHub CLI) on just one repo, so the instance can't reach anything else.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import vastly
from vastly import dim, yellow
from vastly.ssh import run_ssh

STATE_FILE = Path.home() / ".vastly" / "deploy-keys.json"

# Deploy keys vastly creates are titled "vastly-<instance id>"
_TITLE_PREFIX = "vastly-"

_GITHUB_URL = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([^/]+)/([^/]+?)(?:\.git)?/?$"
)


class GitHubError(Exception):
    """A GitHub CLI call failed."""


def github_repo(repo_url: str) -> str | None:
    """Return 'owner/repo' for a github.com remote URL, or None."""
    m = _GITHUB_URL.match(repo_url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def key_title(inst_id: int) -> str:
    """Title of the deploy key vastly adds for an instance."""
    return f"{_TITLE_PREFIX}{inst_id}"


def ssh_url(repo: str) -> str:
    """Return the SSH clone URL for 'owner/repo' (deploy keys only work over SSH)."""
    return f"git@github.com:{repo}.git"


# ── GitHub CLI ──────────────────────────────────────────────────────


def _gh(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the GitHub CLI. Failures (including timeouts) come back as returncode 1."""
    cmd = ["gh", *args]
    try:
        return subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=str(e))


def _error(result: subprocess.CompletedProcess[str]) -> str:
    """First line of a failed command's output, for error messages."""
    text = result.stderr.strip() or result.stdout.strip() or "unknown error"
    return text.splitlines()[0]


def deploy_key_blocker(repo: str | None) -> str | None:
    """Return why vastly can't add a deploy key to *repo*, or None if it can."""
    if repo is None:
        return "the repo isn't on GitHub"
    if not shutil.which("gh"):
        return "the GitHub CLI (gh) isn't installed"
    if _gh("auth", "status", "--hostname", "github.com").returncode != 0:
        return "the GitHub CLI isn't logged in (run: gh auth login)"
    result = _gh("api", f"repos/{repo}", "--jq", ".permissions.admin")
    if result.returncode != 0:
        return f"couldn't check {repo} on GitHub ({_error(result)})"
    if result.stdout.strip() != "true":
        return f"you're not an admin of {repo}"
    return None


def add_deploy_key(repo: str, title: str, public_key: str) -> int:
    """Register *public_key* as a read-write deploy key on *repo*. Returns its ID.

    Reuses an existing deploy key with the same key material, so re-running
    setup doesn't create duplicates. Raises GitHubError on failure.
    """
    material = " ".join(public_key.split()[:2])  # drop the comment

    listing = _gh("api", f"repos/{repo}/keys?per_page=100")
    if listing.returncode != 0:
        raise GitHubError(_error(listing))
    try:
        existing = json.loads(listing.stdout)
    except json.JSONDecodeError as e:
        raise GitHubError("GitHub returned invalid data") from e
    for key in existing:
        if " ".join(key.get("key", "").split()[:2]) == material:
            return key["id"]

    body = json.dumps({"title": title, "key": material, "read_only": False})
    result = _gh("api", "-X", "POST", f"repos/{repo}/keys", "--input", "-", stdin=body)
    if result.returncode != 0:
        raise GitHubError(_error(result))
    try:
        return json.loads(result.stdout)["id"]
    except (json.JSONDecodeError, KeyError) as e:
        raise GitHubError("GitHub returned invalid data") from e


def delete_deploy_key(repo: str, key_id: int) -> bool:
    """Delete a deploy key. Returns True if it's gone (including already gone)."""
    result = _gh("api", "-X", "DELETE", f"repos/{repo}/keys/{key_id}")
    return result.returncode == 0 or "HTTP 404" in result.stderr


def account_id() -> int | None:
    """Vast.ai user ID of the account vastai's API key belongs to, or None."""
    try:
        result = subprocess.run(
            ["vastai", "show", "user", "--raw"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            return None
        return int(json.loads(result.stdout)["id"])
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return None


# ── Instance side ───────────────────────────────────────────────────


def ensure_instance_key(host: str, repo_name: str, inst_id: int) -> str:
    """Create the repo's key pair on the instance if missing; return the public key.

    The private key never leaves the instance. The path must match
    setup-remote.sh ($HOME/.ssh/vastly-<repo_name>) -- keep in sync.
    """
    cmd = (
        f"k=~/.ssh/vastly-{shlex.quote(repo_name)}; "
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f'{{ [ -f "$k" ] || ssh-keygen -q -t ed25519 -N \'\' -C {key_title(inst_id)} -f "$k"; }} && '
        'cat "$k.pub"'
    )
    result = run_ssh(host, cmd)
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("ssh-")]
    if result.returncode != 0 or not lines:
        raise GitHubError(f"couldn't create a key on the instance ({_error(result)})")
    return lines[-1]


# ── Local state (~/.vastly/deploy-keys.json) ────────────────────────
#
# {"<instance id>": {"account": <vast user id>, "agent": bool,
#                    "keys": [{"repo": "owner/repo", "id": <deploy key id>}]}}
#
# "agent" is True when a repo on the instance relies on agent forwarding.


def load_state() -> dict[str, dict]:
    """Load deploy key state. Returns {} if missing or unreadable."""
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        vastly.verbose(f"Warning: could not read {STATE_FILE}, ignoring it")
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict[str, dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _entry(state: dict[str, dict], inst_id: int, account: int | None) -> dict:
    entry = state.setdefault(str(inst_id), {"agent": False, "keys": []})
    if account is not None:
        entry["account"] = account
    return entry


def record_deploy_key(
    inst_id: int, repo: str, key_id: int, account: int | None
) -> None:
    """Remember a deploy key vastly added for a repo on an instance."""
    state = load_state()
    entry = _entry(state, inst_id, account)
    others = [k for k in entry["keys"] if k["repo"] != repo]
    entry["keys"] = [*others, {"repo": repo, "id": key_id}]
    _save_state(state)


def record_agent(inst_id: int, account: int | None) -> None:
    """Remember that a repo on this instance relies on agent forwarding."""
    state = load_state()
    _entry(state, inst_id, account)["agent"] = True
    _save_state(state)


def is_tracked(inst_id: int) -> bool:
    """Whether vastly has deploy key / forwarding state for this instance."""
    return str(inst_id) in load_state()


def forward_agent(
    inst_id: int, mode: str, state: dict[str, dict] | None = None
) -> bool:
    """Whether SSH connections to this instance should forward your SSH agent.

    Always in "agent" mode. Otherwise only when a repo on the instance relies
    on it -- or when vastly has no record of the instance (not set up yet, or
    set up before deploy keys existed), so nothing that worked before breaks.
    """
    if mode == "agent":
        return True
    entry = (load_state() if state is None else state).get(str(inst_id))
    return True if entry is None else bool(entry.get("agent", True))


# ── Cleanup ─────────────────────────────────────────────────────────


def _delete_keys(entry: dict) -> None:
    """Delete an entry's deploy keys from GitHub, printing what happened."""
    for key in entry.get("keys", []):
        repo, key_id = key["repo"], key["id"]
        if shutil.which("gh") and delete_deploy_key(repo, key_id):
            print(dim(f"  Removed deploy key from {repo}"))
        else:
            print(
                yellow(
                    f"  Couldn't remove deploy key {key_id} from {repo}. "
                    f"Remove it at https://github.com/{repo}/settings/keys"
                )
            )


def remove_instance_keys(inst_id: int) -> None:
    """Delete a (destroyed) instance's deploy keys from GitHub and forget them."""
    state = load_state()
    entry = state.pop(str(inst_id), None)
    if entry is None:
        return
    _delete_keys(entry)
    _save_state(state)


def prune(existing_ids: set[str]) -> None:
    """Clean up after instances that no longer exist (e.g. destroyed on the website).

    Entries without keys are just forgotten. Keys are only deleted for
    instances on the current Vast.ai account: an instance missing from the
    list might belong to another account whose API key you've switched away
    from, and its deploy keys must keep working.
    """
    state = load_state()
    gone = [inst_id for inst_id in state if inst_id not in existing_ids]
    if not gone:
        return

    account = None
    if any(state[i].get("keys") for i in gone):
        account = account_id()

    changed = False
    for inst_id in gone:
        entry = state[inst_id]
        if entry.get("keys"):
            if account is None or entry.get("account") != account:
                continue  # can't be sure it's destroyed -- keep it
            _delete_keys(entry)
        del state[inst_id]
        changed = True

    if changed:
        _save_state(state)
