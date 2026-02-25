import sys

__version__ = "0.2.4"

_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def red(s):
    return f"\033[31m{s}\033[0m" if _COLOR else s


def green(s):
    return f"\033[32m{s}\033[0m" if _COLOR else s


def yellow(s):
    return f"\033[33m{s}\033[0m" if _COLOR else s


def cyan(s):
    return f"\033[36m{s}\033[0m" if _COLOR else s


def dim(s):
    return f"\033[90m{s}\033[0m" if _COLOR else s
