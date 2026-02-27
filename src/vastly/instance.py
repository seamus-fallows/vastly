"""Vast.ai instance fetching, naming, display, and selection."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import vastly
from vastly import dim, red, yellow
from vastly.errors import APIError, VastlyError
from vastly.ssh import (
    SSH_CONFIG_DIR,
    cached_config_names,
    clear_ssh_configs,
    ensure_ssh_include,
    find_available_port,
    write_ssh_config,
)

_ALIASES_FILE = Path.home() / ".vastly" / "aliases.json"

# Reserved names that cannot be used as aliases
_RESERVED_NAMES = {"connect", "stop", "destroy", "cp", "name", "list"}


def fetch_instances() -> list[dict]:
    """Call vastai CLI and return list of running instances.

    Raises APIError if the API is unreachable or returns invalid data.
    """
    result = subprocess.run(
        ["vastai", "show", "instances", "--raw"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if result.stderr.strip():
            raise APIError(f"vastai: {result.stderr.strip()}")
        raise APIError(
            "vastai command failed. Is your API key set? Run: vastai set api-key <key>"
        )
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as e:
        raise APIError("vastai returned invalid data") from e

    if not isinstance(data, list):
        raise APIError("vastai returned invalid data")
    return data


def build_instance_name(inst: dict, seen: set[str]) -> str:
    """Generate a human-readable name like '1xRTX4090-TW'.

    Appends the instance ID on collision.
    """
    gpu_name = re.sub(r"\s+", "", inst.get("gpu_name", ""))
    num_gpus = inst.get("num_gpus", 1)
    geo = inst.get("geolocation", "")

    m = re.search(r",\s*(\w+)$", geo)
    country = m.group(1) if m else ""

    base = f"{num_gpus}x{gpu_name}-{country}" if country else f"{num_gpus}x{gpu_name}"

    if base in seen:
        name = f"{base}-{inst['id']}"
    else:
        seen.add(base)
        name = base

    return name


def load_aliases() -> dict[str, str]:
    """Load aliases from ~/.vastly/aliases.json.

    Returns a mapping of instance ID (string) to alias name.
    """
    if not _ALIASES_FILE.exists():
        return {}
    try:
        return json.loads(_ALIASES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_aliases(aliases: dict[str, str]) -> None:
    """Save aliases to ~/.vastly/aliases.json."""
    _ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ALIASES_FILE.write_text(json.dumps(aliases, indent=2) + "\n", encoding="utf-8")


def validate_alias(alias: str, instances: list[dict], aliases: dict[str, str]) -> None:
    """Validate an alias name. Raises VastlyError on invalid input."""
    if not alias:
        raise VastlyError("Alias cannot be empty.")
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", alias):
        raise VastlyError(
            f"Invalid alias '{alias}'. Must be lowercase alphanumeric with hyphens."
        )
    if alias in _RESERVED_NAMES:
        raise VastlyError(f"'{alias}' is a reserved command name and cannot be used as an alias.")

    # Check collision with auto-generated names
    auto_names = {i["name"] for i in instances}
    if alias in auto_names:
        raise VastlyError(f"'{alias}' conflicts with an auto-generated instance name.")

    # Check collision with other aliases
    for inst_id, existing_alias in aliases.items():
        if existing_alias == alias:
            raise VastlyError(f"Alias '{alias}' is already assigned to instance {inst_id}.")


def format_uptime(unix_ts) -> str:
    """Format a Unix timestamp as a short uptime string ('3.1h' or '10m')."""
    if not unix_ts:
        return "?"
    started = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    seconds = (now - started).total_seconds()
    hours = seconds / 3600
    if hours >= 1:
        return f"{round(hours, 1)}h"
    return f"{round(seconds / 60)}m"


def sync_instances(config: dict) -> list[dict]:
    """Sync running instances from the API to SSH configs.

    Returns a list of instance dicts with keys: name, id, dph_total,
    gpu_name, num_gpus, start_date, cached.

    Raises APIError if the API is unreachable and no cached configs exist.
    """
    ensure_ssh_include()
    SSH_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    print(dim("Syncing instances..."))
    try:
        all_instances = fetch_instances()
    except APIError:
        cached = cached_config_names()
        if cached:
            print(yellow("API unreachable -- using cached configs."))
            return [
                {
                    "name": n,
                    "cached": True,
                    "id": None,
                    "dph_total": 0,
                    "gpu_name": "",
                    "num_gpus": 0,
                    "start_date": None,
                }
                for n in cached
            ]
        raise

    running = [i for i in all_instances if i.get("cur_state") == "running"]
    vastly.verbose(f"API returned {len(all_instances)} instances, {len(running)} running")

    clear_ssh_configs()

    if not running:
        return []

    seen: set[str] = set()
    used_ports: set[int] = set()
    results = []

    for inst in running:
        name = build_instance_name(inst, seen)

        # Get SSH port -- skip instances without port 22 exposed
        try:
            ssh_port = inst["ports"]["22/tcp"][0]["HostPort"]
        except (KeyError, IndexError, TypeError):
            continue

        local_forwards = []
        for pf in config["portForwards"]:
            local_port = find_available_port(int(pf["local"]), used_ports)
            used_ports.add(local_port)
            local_forwards.append((local_port, int(pf["remote"])))

        write_ssh_config(
            name,
            host=inst["public_ipaddr"],
            port=int(ssh_port),
            user=config["sshUser"],
            key_path=config["sshKeyPath"],
            local_forwards=local_forwards,
        )

        results.append(
            {
                "name": name,
                "id": inst["id"],
                "dph_total": inst.get("dph_total", 0),
                "gpu_name": inst.get("gpu_name", ""),
                "num_gpus": inst.get("num_gpus", 0),
                "start_date": inst.get("start_date"),
                "cached": False,
                "alias": None,
                # Store SSH config params for alias config generation
                "_host": inst["public_ipaddr"],
                "_port": int(ssh_port),
                "_user": config["sshUser"],
                "_key_path": config["sshKeyPath"],
                "_local_forwards": local_forwards,
            }
        )

    # Write alias SSH configs and attach aliases to instance dicts
    aliases = load_aliases()
    active_ids = {str(r["id"]) for r in results}
    stale_ids = [k for k in aliases if k not in active_ids]
    if stale_ids:
        for k in stale_ids:
            del aliases[k]
        save_aliases(aliases)

    for r in results:
        inst_id = str(r["id"])
        alias = aliases.get(inst_id)
        if alias:
            r["alias"] = alias
            write_ssh_config(
                alias,
                host=r["_host"],
                port=r["_port"],
                user=r["_user"],
                key_path=r["_key_path"],
                local_forwards=r["_local_forwards"],
            )

    # Clean up internal keys not needed by callers
    for r in results:
        for k in ("_host", "_port", "_user", "_key_path", "_local_forwards"):
            del r[k]

    return results


def get_synced_instances(config: dict) -> list[dict]:
    """Sync and return instances.

    Raises VastlyError if no running instances exist.
    Raises APIError if the API is unreachable and no cached configs exist.
    """
    result = sync_instances(config)
    if not result:
        raise VastlyError("No running Vast instances.")
    return result


def show_table(instances: list[dict]) -> None:
    """Print a compact instance table."""
    for inst in instances:
        if inst["cached"]:
            print(f"  {inst['name']}  {dim('(cached)')}")
        else:
            alias = inst.get("alias")
            label = f"{alias} ({inst['name']})" if alias else inst["name"]
            cost = f"${inst['dph_total']:.2f}/hr"
            uptime = format_uptime(inst["start_date"])
            print(f"  {label}   {cost}   {uptime} uptime")
    print()


def _display_name(inst: dict) -> str:
    """Return the display name for an instance (alias or auto-generated name)."""
    alias = inst.get("alias")
    return f"{alias} ({inst['name']})" if alias else inst["name"]


def select_instance(instances: list[dict], name: str | None = None) -> list[dict]:
    """Select instance(s) by name or alias, auto-select if only one, or prompt interactively."""
    if name:
        match = [i for i in instances if i["name"] == name or i.get("alias") == name]
        if not match:
            labels = [_display_name(i) for i in instances]
            raise VastlyError(f"No instance named '{name}'. Available: {', '.join(labels)}")
        return match

    names = [_display_name(i) for i in instances]

    if len(instances) == 1:
        return [instances[0]]

    # Interactive selection
    print("Select instance:")
    for i, n in enumerate(names):
        print(f"  [{i + 1}] {n}")
    print("  [a] All")

    try:
        choice = input("Choice: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return []

    if choice == "a":
        return list(instances)

    if not choice.isdigit():
        return []

    idx = int(choice)
    if idx < 1 or idx > len(names):
        return []

    return [instances[idx - 1]]
