"""IDE launch helpers."""

import shutil
import subprocess
import sys


def check_ide(ide: str) -> bool:
    return shutil.which(ide) is not None


def open_ide(ide: str, host: str, remote_path: str) -> None:
    """Launch the IDE connected to *host* at *remote_path* via Remote-SSH."""
    # On Windows, IDE commands (code, cursor) are .cmd scripts that need the shell.
    # Popen so we don't block -- the IDE runs independently.
    subprocess.Popen(
        [ide, "--remote", f"ssh-remote+{host}", remote_path],
        shell=(sys.platform == "win32"),
    )
