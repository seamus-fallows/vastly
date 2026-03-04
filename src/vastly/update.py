"""Check PyPI for newer versions of vastly."""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from vastly import __version__, dim

_CACHE_DIR = Path.home() / ".vastly"
_CACHE_FILE = _CACHE_DIR / ".last-update-check"
_CHECK_INTERVAL = 7 * 24 * 3600  # 7 days


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a dotted version string into a tuple of ints for comparison.

    Strips pre-release suffixes (e.g. '0a1' -> 0, '4rc2' -> 4) so that
    versions like '0.4.0a1' or '1.0.0.dev1' don't raise ValueError.
    """
    parts = []
    for segment in v.split("."):
        # Take only the leading digits from each segment
        digits = ""
        for ch in segment:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts[:3])


def check_for_update() -> None:
    """Print a message if a newer version is available on PyPI.

    Checks at most once per 7 days.  Any error (no internet, PyPI down,
    malformed response) is silently swallowed.
    """
    try:
        # Rate-limit: skip if checked recently
        if _CACHE_FILE.exists():
            last_check = float(_CACHE_FILE.read_text(encoding="utf-8").strip())
            if time.time() - last_check < _CHECK_INTERVAL:
                return

        req = urllib.request.Request(
            "https://pypi.org/pypi/vastly/json",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())

        latest = data["info"]["version"]

        # Update cache timestamp regardless of whether update is available
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(str(time.time()), encoding="utf-8")

        if _parse_version(latest) > _parse_version(__version__):
            print(
                dim(
                    f"  vastly {latest} available (you have {__version__}). "
                    "Update: pip install -U vastly"
                )
            )
    except Exception:
        pass
