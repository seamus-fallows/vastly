"""The Vast.ai REST API, called directly (standard library only).

vastly uses the same API key as the vastai CLI -- VAST_API_KEY, or the CLI's
key file -- so the two stay on the same account. The CLI itself isn't needed.
"""

from __future__ import annotations

import getpass
import http.client
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import vastly
from vastly import __version__, dim
from vastly.errors import APIError, VastlyError

_API = "https://console.vast.ai"
KEYS_PAGE = "https://cloud.vast.ai/manage-keys/"

# Where older versions of the vastai CLI kept the key
LEGACY_KEY_FILE = Path.home() / ".vast_api_key"

_TIMEOUT = 30
# Connection problems and these statuses are retried after each delay (seconds)
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_DELAYS = (2.0, 5.0)

_CERT_HELP = (
    "Couldn't verify Vast.ai's HTTPS certificate: this Python can't find CA "
    "certificates. With Python from python.org on macOS, run 'Install "
    "Certificates.command' (in Applications > Python 3.x)."
)


# ── API key ─────────────────────────────────────────────────────────


def key_file() -> Path:
    """The vastai CLI's key file, which vastly shares."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "vastai" / "vast_api_key"


def saved_key() -> str | None:
    """The API key from VAST_API_KEY or the vastai CLI's key file, or None."""
    key = os.environ.get("VAST_API_KEY", "").strip()
    if key:
        return key
    for path in (key_file(), LEGACY_KEY_FILE):
        try:
            key = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if key:
            return key
    return None


def api_key() -> str:
    """The saved API key, or one asked for now (the first time vastly runs)."""
    return saved_key() or ask_for_key()


def ask_for_key() -> str:
    """Ask for an API key, check it with Vast.ai, and save it to key_file()."""
    print(f"vastly needs a Vast.ai API key. Create one at {KEYS_PAGE}")
    try:
        if sys.stdin.isatty():
            key = getpass.getpass("API key (hidden): ")
        else:  # piped, or a terminal getpass can't use (Git Bash on Windows)
            key = input("API key: ")
    except (EOFError, KeyboardInterrupt):
        print()
        raise VastlyError(
            "No Vast.ai API key. Run 'vst config --api-key' to enter one, "
            "or set VAST_API_KEY."
        ) from None
    key = key.strip()
    if not key:
        raise VastlyError("No key entered.")
    user = current_user(key=key)
    save_key(key)
    account = account_label(user)
    print(
        dim(f"  Saved to {key_file()}" + (f" (account: {account})" if account else ""))
    )
    return key


def save_key(key: str) -> None:
    """Write the key where the vastai CLI keeps it, readable only by you."""
    path = key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    os.chmod(path, 0o600)  # the vastai CLI creates it world-readable


def account_label(user: dict[str, Any]) -> str | None:
    """Describe an account as 'username (email, team)', or None if it has neither."""
    name = user.get("username") or user.get("email")
    if not name:
        return None
    details = []
    if user.get("email") and user["email"] != name:
        details.append(user["email"])
    if user.get("is_team"):
        details.append("team")
    return f"{name} ({', '.join(details)})" if details else name


# ── Requests ────────────────────────────────────────────────────────


def _send(method: str, url: str, body: dict | None, key: str) -> tuple[int, bytes]:
    """One HTTP request: (status, body). vastly's only network call to Vast.ai."""
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": f"vastly/{__version__}",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read()


def _reason(raw: bytes) -> str:
    """The message in an API reply, for errors."""
    text = raw.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        for field in ("msg", "error", "message"):
            if data.get(field):
                return str(data[field])
    return text[:200] or "no details"


def _parse(status: int, raw: bytes) -> dict[str, Any]:
    if status in (401, 403):
        raise APIError(
            f"Vast.ai refused the API key (HTTP {status}: {_reason(raw)}). "
            "Run 'vst config --api-key' to enter a different one.",
            status,
        )
    if not 200 <= status < 300:
        raise APIError(f"Vast.ai returned HTTP {status}: {_reason(raw)}", status)
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise APIError("Vast.ai returned invalid data.", status)
    return data


def _request(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    query: dict[str, str] | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    """Call the API and return its JSON reply.

    Connection problems and server errors are retried: every call vastly
    makes is safe to repeat. Raises APIError.
    """
    url = _API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    key = key or api_key()
    problem = ""
    for delay in (*_RETRY_DELAYS, None):
        try:
            status, raw = _send(method, url, body, key)
        except (OSError, http.client.HTTPException) as e:
            reason = getattr(e, "reason", None) or e  # URLError wraps the cause
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise APIError(_CERT_HELP) from None
            problem = str(reason) or type(reason).__name__
        else:
            if status not in _RETRY_STATUS:
                return _parse(status, raw)
            problem = f"HTTP {status}: {_reason(raw)}"
        if delay is not None:
            vastly.verbose(
                f"Vast.ai request failed ({problem}), retrying in {delay:.0f}s"
            )
            time.sleep(delay)
    raise APIError(
        f"Couldn't reach Vast.ai ({problem}). "
        "Check your connection, or try again in a minute."
    )


# ── Endpoints ───────────────────────────────────────────────────────


def list_instances() -> list[dict[str, Any]]:
    """Every instance on the account, in any state."""
    data = _request("GET", "/api/v0/instances/", query={"owner": "me"})
    instances = data.get("instances")
    if not isinstance(instances, list):
        raise APIError("Vast.ai returned invalid data.")
    return [i for i in instances if isinstance(i, dict)]


def get_instance(inst_id: int) -> dict[str, Any] | None:
    """One instance, or None if it no longer exists."""
    try:
        data = _request("GET", f"/api/v0/instances/{inst_id}/", query={"owner": "me"})
    except APIError as e:
        if e.status == 404:
            return None
        raise
    if "instances" not in data:  # a glitch mustn't look like "gone"
        raise APIError("Vast.ai returned invalid data.")
    inst = data["instances"]
    return inst if isinstance(inst, dict) and inst.get("id") is not None else None


def start_instance(inst_id: int) -> bool:
    """Start an instance. True if Vast.ai queued the start (its machine is busy)."""
    reply = _request("PUT", f"/api/v0/instances/{inst_id}/", {"state": "running"})
    message = str(reply.get("msg") or "").lower()
    if "queue" in message or "unavailable" in message:
        return True
    _check(reply)
    return False


def stop_instance(inst_id: int) -> None:
    _check(_request("PUT", f"/api/v0/instances/{inst_id}/", {"state": "stopped"}))


def destroy_instance(inst_id: int) -> None:
    """Destroy an instance (irreversible). One that's already gone is fine."""
    try:
        _check(_request("DELETE", f"/api/v0/instances/{inst_id}/", {}))
    except APIError as e:
        if e.status != 404:
            raise


def current_user(key: str | None = None) -> dict[str, Any]:
    """The account the API key (or *key*) belongs to."""
    return _request("GET", "/api/v0/users/current", key=key)


def _check(reply: dict[str, Any]) -> None:
    """Raise if Vast.ai answered but said the change didn't happen."""
    if reply.get("success") is False:
        raise APIError(str(reply.get("msg") or reply.get("error") or "Vast.ai refused"))
