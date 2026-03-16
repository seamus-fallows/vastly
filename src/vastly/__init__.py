import sys

__version__ = "0.4.1"

VERBOSE: bool = False

_COLOR: bool = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _COLOR else s


def green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _COLOR else s


def yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _COLOR else s


def cyan(s: str) -> str:
    return f"\033[36m{s}\033[0m" if _COLOR else s


def dim(s: str) -> str:
    return f"\033[90m{s}\033[0m" if _COLOR else s


def verbose(s: str) -> None:
    """Print a message only when verbose mode is enabled."""
    if VERBOSE:
        print(dim(s), file=sys.stderr)
