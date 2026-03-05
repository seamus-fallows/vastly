"""Vast.ai instance fetching, naming, display, and selection."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import vastly
from vastly.config import Config
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


@dataclass
class Instance:
    """A Vast.ai GPU instance."""

    name: str
    id: int | None  # None only for cached fallback instances
    dph_total: float
    gpu_name: str
    num_gpus: int
    start_date: float | None  # Unix timestamp
    cached: bool
    status: str  # "running", "stopped", "exited", etc.
    alias: str | None

    @property
    def display_name(self) -> str:
        """Return alias (auto-name) if aliased, otherwise just name."""
        return f"{self.alias} ({self.name})" if self.alias else self.name

_ALIASES_FILE = Path.home() / ".vastly" / "aliases.json"

# Reserved names that cannot be used as aliases
_RESERVED_NAMES = {
    "connect",
    "stop",
    "destroy",
    "cp",
    "name",
    "list",
    "start",
    "config",
    "ssh",
}

# Instance lifecycle state classifications
STOPPABLE_STATES = {"running", "scheduling", "loading", "creating", "connecting"}
STOPPED_STATES = {"stopped", "exited"}
TRANSITIONAL_STATES = {"loading", "creating", "connecting", "scheduling"}


def fetch_instances() -> list[dict[str, Any]]:
    """Call vastai CLI and return list of running instances.

    Raises APIError if the API is unreachable or returns invalid data.
    """
    try:
        result = subprocess.run(
            ["vastai", "show", "instances", "--raw"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise APIError("vastai command timed out. Check your internet connection.")
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


def build_instance_name(inst: dict[str, Any], seen: set[str]) -> str:
    """Generate a human-readable name like '1xRTX4090-TW'.

    Appends the instance ID on collision.
    """
    gpu_name = re.sub(r"\s+", "", inst.get("gpu_name", "")) or "unknown"
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
        vastly.verbose(f"Warning: could not read {_ALIASES_FILE}, using empty aliases")
        return {}


def save_aliases(aliases: dict[str, str]) -> None:
    """Save aliases to ~/.vastly/aliases.json."""
    _ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ALIASES_FILE.write_text(json.dumps(aliases, indent=2) + "\n", encoding="utf-8")


def validate_alias(alias: str, instances: list[Instance], aliases: dict[str, str]) -> None:
    """Validate an alias name. Raises VastlyError on invalid input."""
    if not alias:
        raise VastlyError("Alias cannot be empty.")
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", alias):
        raise VastlyError(
            f"Invalid alias '{alias}'. Must be lowercase alphanumeric with hyphens."
        )
    if alias in _RESERVED_NAMES:
        raise VastlyError(
            f"'{alias}' is a reserved command name and cannot be used as an alias."
        )

    # Check collision with auto-generated names
    auto_names = {i.name for i in instances}
    if alias in auto_names:
        raise VastlyError(f"'{alias}' conflicts with an auto-generated instance name.")

    # Check collision with other aliases
    for inst_id, existing_alias in aliases.items():
        if existing_alias == alias:
            raise VastlyError(
                f"Alias '{alias}' is already assigned to instance {inst_id}."
            )


def format_uptime(unix_ts: float | str | None) -> str:
    """Format a Unix timestamp as a short uptime string ('3.1h' or '10m')."""
    if not unix_ts:
        return "?"
    if isinstance(unix_ts, str):
        try:
            unix_ts = float(unix_ts)
        except ValueError:
            return "?"
    started = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    seconds = (now - started).total_seconds()
    hours = seconds / 3600
    if hours >= 1:
        return f"{round(hours, 1)}h"
    return f"{round(seconds / 60)}m"


def sync_instances(config: Config) -> list[Instance]:
    """Sync instances from the API to SSH configs.

    Returns a list of Instance objects for ALL states (running, stopped,
    exited, etc.). SSH configs are only written for running instances.

    Raises APIError if the API is unreachable and no cached configs exist.
    """
    ensure_ssh_include()
    SSH_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    print(dim("Syncing instances..."))
    try:
        all_api = fetch_instances()
    except APIError:
        cached = cached_config_names()
        if cached:
            print(yellow("API unreachable -- using cached configs."))
            return [
                Instance(
                    name=n, id=None, dph_total=0, gpu_name="", num_gpus=0,
                    start_date=None, cached=True, status="running", alias=None,
                )
                for n in cached
            ]
        raise

    running = [i for i in all_api if i.get("cur_state") == "running"]
    non_running = [i for i in all_api if i.get("cur_state") != "running"]
    vastly.verbose(f"API returned {len(all_api)} instances, {len(running)} running")

    clear_ssh_configs()

    seen: set[str] = set()
    used_ports: set[int] = set()
    results: list[Instance] = []
    # SSH config params for running instances, keyed by instance ID.
    # Used later to write alias configs without leaking internal data onto Instance.
    ssh_params: dict[int, dict] = {}

    # Process running instances first (they get SSH configs and clean names)
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

        ssh_params[inst["id"]] = {
            "host": inst["public_ipaddr"],
            "port": int(ssh_port),
            "user": config["sshUser"],
            "key_path": config["sshKeyPath"],
            "local_forwards": local_forwards,
        }

        results.append(Instance(
            name=name,
            id=inst["id"],
            dph_total=inst.get("dph_total", 0),
            gpu_name=inst.get("gpu_name", ""),
            num_gpus=inst.get("num_gpus", 0),
            start_date=inst.get("start_date"),
            cached=False,
            status="running",
            alias=None,
        ))

    # Process non-running instances (no SSH config, no port extraction)
    for inst in non_running:
        name = build_instance_name(inst, seen)
        results.append(Instance(
            name=name,
            id=inst["id"],
            dph_total=inst.get("dph_total", 0),
            gpu_name=inst.get("gpu_name", ""),
            num_gpus=inst.get("num_gpus", 0),
            start_date=inst.get("start_date"),
            cached=False,
            status=inst.get("cur_state", "unknown"),
            alias=None,
        ))

    # Prune aliases for instances that no longer exist in the API at all
    aliases = load_aliases()
    all_api_ids = {str(r.id) for r in results}
    stale_ids = [k for k in aliases if k not in all_api_ids]
    if stale_ids:
        for k in stale_ids:
            del aliases[k]
        save_aliases(aliases)

    # Write alias SSH configs (running only) and attach aliases to all instances
    for r in results:
        if r.id is None:
            continue
        inst_id = str(r.id)
        alias = aliases.get(inst_id)
        if alias:
            r.alias = alias
            if r.status == "running" and r.id in ssh_params:
                params = ssh_params[r.id]
                write_ssh_config(
                    alias,
                    host=params["host"],
                    port=params["port"],
                    user=params["user"],
                    key_path=params["key_path"],
                    local_forwards=params["local_forwards"],
                )

    return results


def get_synced_instances(config: Config) -> list[Instance]:
    """Sync and return all instances (any state).

    Raises VastlyError if no instances exist at all.
    Raises APIError if the API is unreachable and no cached configs exist.
    """
    result = sync_instances(config)
    if not result:
        raise VastlyError("No Vast instances found.")
    return result


def get_running_instances(config: Config) -> list[Instance]:
    """Sync and return only running instances.

    Raises VastlyError if no running instances exist. When stopped/exited
    instances exist, the error message suggests vst list and vst start.
    """
    all_instances = sync_instances(config)
    running = [i for i in all_instances if i.status == "running"]
    if not running:
        stopped = [i for i in all_instances if i.status != "running"]
        if stopped:
            raise VastlyError(
                f"No running instances. {len(stopped)} stopped/exited. "
                "Use 'vst list' to see all, 'vst start' to restart."
            )
        raise VastlyError("No Vast instances found.")
    return running


def show_table(instances: list[Instance]) -> None:
    """Print a compact, column-aligned instance table."""
    if not instances:
        print()
        return

    # Build rows: (label, cost, right_col, is_running, is_cached)
    rows: list[tuple[str, str, str, bool, bool]] = []
    for inst in instances:
        if inst.cached:
            rows.append((inst.name, "", "(cached)", False, True))
        else:
            label = inst.display_name
            cost = f"${inst.dph_total:.2f}/hr"
            if inst.status == "running":
                rows.append(
                    (
                        label,
                        cost,
                        f"{format_uptime(inst.start_date)} uptime",
                        True,
                        False,
                    )
                )
            else:
                rows.append((label, cost, inst.status, False, False))

    label_w = max(len(r[0]) for r in rows)
    cost_w = max((len(r[1]) for r in rows), default=0)

    for label, cost, right, is_running, is_cached in rows:
        if is_cached:
            print(f"  {label.ljust(label_w)}  {dim('(cached)')}")
        elif is_running:
            print(f"  {label.ljust(label_w)}  {cost.ljust(cost_w)}  {right}")
        else:
            print(dim(f"  {label.ljust(label_w)}  {cost.ljust(cost_w)}  {right}"))
    print()


def select_instance(
    instances: list[Instance],
    name: str | None = None,
    *,
    allow_all: bool = False,
) -> list[Instance]:
    """Select instance(s) by name/alias, auto-select if only one, or prompt.

    When allow_all is True, shows an "[a] All" option for multi-instance selection.
    Always returns a non-empty list. Raises VastlyError on cancel or invalid input.
    """
    if name:
        match = [i for i in instances if i.name == name or i.alias == name]
        if not match:
            labels = [i.display_name for i in instances]
            raise VastlyError(
                f"No instance named '{name}'. Available: {', '.join(labels)}"
            )
        vastly.verbose(f"Matched instance by name/alias: {match[0].display_name}")
        return match

    if len(instances) == 1:
        vastly.verbose(f"Auto-selected sole instance: {instances[0].display_name}")
        return [instances[0]]

    # Interactive selection
    labels = [i.display_name for i in instances]
    print("Select instance:")
    for i, label in enumerate(labels):
        print(f"  [{i + 1}] {label}")
    if allow_all:
        print("  [a] All")

    try:
        choice = input("Choice: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise VastlyError("No instance selected.")

    if allow_all and choice == "a":
        return list(instances)

    if not choice.isdigit():
        raise VastlyError("No instance selected.")

    idx = int(choice)
    if idx < 1 or idx > len(instances):
        raise VastlyError("No instance selected.")

    return [instances[idx - 1]]
