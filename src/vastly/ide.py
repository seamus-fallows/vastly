"""IDE launch helpers."""

import shutil
import subprocess
import sys


def check_ide(ide: str) -> bool:
    """Return True if *ide* is found on PATH."""
    return shutil.which(ide) is not None


def open_ide(ide: str, host: str, remote_path: str) -> None:
    """Launch the IDE connected to *host* at *remote_path* via Remote-SSH."""
    cmd = [ide, "--remote", f"ssh-remote+{host}", remote_path]
    # On Windows, IDE commands (code, cursor) are .cmd scripts that need the shell.
    # list2cmdline properly quotes args with spaces, unlike Popen(list, shell=True)
    # which naively joins them.
    if sys.platform == "win32":
        subprocess.Popen(subprocess.list2cmdline(cmd), shell=True)
    else:
        subprocess.Popen(cmd)
