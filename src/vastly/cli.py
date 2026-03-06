"""CLI entry point for the `vst` command."""

from __future__ import annotations

import argparse
import sys

import vastly
from vastly import __version__, cyan, dim, green, red
from vastly.config import CONFIG_PATH, ensure_config
from vastly.commands import (
    cmd_config,
    cmd_connect,
    cmd_cp,
    cmd_destroy,
    cmd_list,
    cmd_name,
    cmd_ssh,
    cmd_start,
    cmd_stop,
)
from vastly.errors import VastlyError


# ── Help system ─────────────────────────────────────────────────────


_CMD_HELP = {
    "connect": {
        "usage": "vst [name] [-n | -f] [--all]",
        "desc": "Connect to an instance and open your IDE.",
        "detail": "First visit: clones your repo, installs deps. Revisits: skips straight to IDE.",
        "examples": [
            ("vst", "auto-select and connect"),
            ("vst train", "connect by alias"),
            ("vst --all", "connect to all instances"),
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
        "usage": "vst stop [name] [--all] [-y]",
        "desc": "Stop a running instance.",
        "examples": [
            ("vst stop", "stop your instance"),
            ("vst stop --all", "stop everything"),
        ],
    },
    "destroy": {
        "usage": "vst destroy [name] [--all] [-y]",
        "desc": "Destroy an instance (irreversible).",
        "detail": "Also removes its SSH config and any alias.",
        "examples": [
            ("vst destroy", "destroy your instance"),
            ("vst destroy --all", "destroy everything"),
            ("vst destroy --all -y", "destroy all without confirmation"),
        ],
    },
    "cp": {
        "usage": "vst cp <up|down> [paths...] [-c] [-i instance]",
        "desc": "Copy files to/from a remote instance.",
        "detail": "Paths are relative to the git repo root. Use -c to copy all copyFiles entries.",
        "examples": [
            ("vst cp up .env", "upload a file"),
            ("vst cp up .env .claude/ data/", "upload multiple paths"),
            ("vst cp up -c", "upload all copyFiles"),
            ("vst cp up -c .env.prod", "copyFiles + extra"),
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
    "ssh": {
        "usage": "vst ssh [name] [command...]",
        "desc": "Open an SSH session to an instance.",
        "detail": "Without a command, opens an interactive shell. With a command, runs it and exits.\n"
        "If the first argument matches an instance name, it connects there.\n"
        "Otherwise it's treated as a command. Use -- to force command interpretation.",
        "examples": [
            ("vst ssh", "interactive shell"),
            ("vst ssh train", "SSH by alias"),
            ("vst ssh nvidia-smi", "run a command"),
            ("vst ssh train nvidia-smi", "run a command on a named instance"),
            ("vst ssh -- -v", "pass flags as a remote command"),
        ],
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
        ("ssh", "SSH into an instance"),
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
    # _actions is private, but argparse has no public equivalent for
    # enumerating a parser's actions with their option_strings and help text.
    for action in parsers[cmd]._actions:
        if action.dest in ("help", "verbose") or not action.help:
            continue
        if action.option_strings:
            opts.append((", ".join(action.option_strings), action.help))
        else:
            name = "|".join(action.choices) if action.choices else (action.metavar or action.dest)
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


# ── Argument parser ─────────────────────────────────────────────────


def _build_parser() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    """Build and return (parser, {cmd_name: subparser})."""
    hint = "auto-selects if only one, prompts if multiple"

    parser = argparse.ArgumentParser(prog="vst", add_help=False)
    parser.add_argument("--version", action="version", version=f"vastly {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--all", action="store_true", help=argparse.SUPPRESS)
    g_top = parser.add_mutually_exclusive_group()
    g_top.add_argument("-n", "--no-setup", action="store_true")
    g_top.add_argument("-f", "--force-setup", action="store_true")

    subparsers = parser.add_subparsers(dest="command")
    parsers: dict[str, argparse.ArgumentParser] = {}

    # Connect (default when no subcommand given)
    p = subparsers.add_parser("connect", help="connect to an instance and open IDE")
    p.add_argument("name", nargs="?", help=f"instance name or alias ({hint})")
    p.add_argument("--all", action="store_true", help="connect to all instances")
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
    parsers["list"] = p

    # Stop
    p = subparsers.add_parser("stop", help="stop a running instance")
    p.add_argument("name", nargs="?", help=f"instance name or alias ({hint})")
    p.add_argument("--all", action="store_true", help="stop all running instances")
    p.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    parsers["stop"] = p

    # Destroy
    p = subparsers.add_parser("destroy", help="destroy an instance (irreversible)")
    p.add_argument("name", nargs="?", help=f"instance name or alias ({hint})")
    p.add_argument("--all", action="store_true", help="destroy all instances")
    p.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    parsers["destroy"] = p

    # Cp
    p = subparsers.add_parser("cp", help="copy files to/from a remote instance")
    p.add_argument("direction", choices=["up", "down"], help="upload or download")
    p.add_argument("paths", nargs="*", help="files or directories to copy")
    p.add_argument(
        "-c", "--config", action="store_true", help="copy all copyFiles entries"
    )
    p.add_argument("-i", "--instance", help=f"target instance ({hint})")
    parsers["cp"] = p

    # Start
    p = subparsers.add_parser("start", help="start a stopped instance")
    p.add_argument("name", nargs="?", help=f"instance name or alias ({hint})")
    p.add_argument(
        "-n", "--no-connect", action="store_true", help="start without connecting"
    )
    parsers["start"] = p

    # Name
    p = subparsers.add_parser("name", help="assign a custom name to an instance")
    p.add_argument("alias", help="name to assign")
    p.add_argument("-i", "--instance", help=f"instance to name ({hint})")
    p.add_argument("--clear", action="store_true", help="remove the alias")
    parsers["name"] = p

    # Config
    p = subparsers.add_parser("config", help="show current configuration")
    parsers["config"] = p

    # SSH
    p = subparsers.add_parser("ssh", help="SSH into a running instance")
    p.add_argument("name", nargs="?", help=f"instance name or alias ({hint})")
    p.add_argument(
        "remote_cmd", nargs=argparse.REMAINDER, help="command to run (optional)",
        metavar="command",
    )
    parsers["ssh"] = p

    return parser, parsers


# ── Entry point ─────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and dispatch to the appropriate subcommand."""
    raw = list(argv if argv is not None else sys.argv[1:])

    if ensure_config():
        print(dim("  \u250c Created ") + str(CONFIG_PATH))
        print(dim("  \u2502 Run 'vst' from a git repo to connect to your Vast.ai instance."))
        print(dim("  \u2502 Run 'vst -h' for all commands."))
        print(dim("  \u2514 Edit ~/.vastly.json to customize.\n"))

    parser, parsers = _build_parser()

    # Handle help ourselves for clean, colored output
    if {"-h", "--help"} & set(raw):
        rest = [a for a in raw if a not in ("-h", "--help")]
        cmd = rest[0] if rest and not rest[0].startswith("-") else None
        if cmd in _CMD_HELP:
            _print_cmd_help(cmd, parsers)
        else:
            _print_help()

    # Treat unknown subcommands as instance names for connect.
    # e.g. `vst my-gpu` becomes `vst connect my-gpu`
    # e.g. `vst -v my-gpu` becomes `vst -v connect my-gpu`
    known = set(parsers.keys())
    non_flags = [a for a in raw if not a.startswith("-")]
    if non_flags and non_flags[0] not in known:
        idx = raw.index(non_flags[0])
        raw.insert(idx, "connect")

    args = parser.parse_args(raw)

    # No subcommand = connect with auto-select
    if args.command is None:
        args.command = "connect"
        args.name = None

    # Argparse subparser defaults override top-level flags when both define
    # the same attribute. These flags are on both the top-level parser (for
    # bare `vst -f`) and the connect subparser, so we scan raw argv to
    # ensure the top-level value isn't lost. Scoped to connect to avoid
    # false matches in other commands' positional/remainder args.
    if args.command == "connect":
        if "-f" in raw or "--force-setup" in raw:
            args.force_setup = True
        if "-n" in raw or "--no-setup" in raw:
            args.no_setup = True
        if "--all" in raw:
            args.all = True

    if args.verbose:
        vastly.VERBOSE = True

    dispatch = {
        "connect": cmd_connect, "list": cmd_list, "start": cmd_start,
        "stop": cmd_stop, "destroy": cmd_destroy, "name": cmd_name,
        "cp": cmd_cp, "config": cmd_config, "ssh": cmd_ssh,
    }

    try:
        handler = dispatch.get(args.command)
        if handler:
            handler(args)
    except KeyboardInterrupt:
        print(file=sys.stderr)  # clean up partial line
        sys.exit(130)  # standard exit code for SIGINT
    except VastlyError as e:
        print(red(str(e)), file=sys.stderr)
        sys.exit(e.exit_code)
