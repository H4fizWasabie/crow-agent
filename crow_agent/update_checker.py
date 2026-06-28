"""Update checker — notifies user when a newer version is available on GitHub.

Non-blocking, cache-friendly, zero-dependency. Fires on app startup.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from urllib.request import urlopen, Request

from . import __version__

logger = logging.getLogger("crow_agent.update")

_GITHUB_RELEASES_URL = (
    "https://api.github.com/repos/H4fizWasabie/crow-agent/releases/latest"
)
_CACHE_PATH = Path.home() / ".crow_agent" / "update_check_cache.json"
_CHECK_INTERVAL = 3600 * 6  # check every 6 hours


def check_for_updates() -> str | None:
    """Check GitHub for a newer release. Returns latest version if newer, None otherwise.

    Caches the last check time — only hits the API once per _CHECK_INTERVAL.
    Never crashes — all failures are logged and swallowed.
    """
    now = time.time()

    # Respect cache interval
    if _CACHE_PATH.exists():
        try:
            cache = json.loads(_CACHE_PATH.read_text())
            if now - cache.get("checked_at", 0) < _CHECK_INTERVAL:
                cached_version = cache.get("latest_version")
                if cached_version and _is_newer(cached_version, __version__):
                    _log_update_available(cached_version)
                    return cached_version
                return None
        except (json.JSONDecodeError, OSError):
            pass

    try:
        req = Request(
            _GITHUB_RELEASES_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "crow-agent"},
        )
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            latest = data.get("tag_name", "").lstrip("v")
    except Exception as e:
        logger.debug("Update check failed (network/API): %s", e)
        return None

    # Cache result
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps({"checked_at": now, "latest_version": latest}))

    if latest and _is_newer(latest, __version__):
        _log_update_available(latest)
        return latest

    return None


def _is_newer(latest: str, current: str) -> bool:
    """Compare semver strings. Returns True if latest > current."""
    try:
        from packaging.version import Version
        return Version(latest) > Version(current)
    except ImportError:
        # Fallback: split and compare numerically
        def _parse(v: str) -> tuple:
            parts = v.split(".")
            return tuple(int(p) for p in parts[:3] if p.isdigit())
        try:
            return _parse(latest) > _parse(current)
        except Exception:
            return latest != current  # false positive is better than false negative


def _log_update_available(latest: str) -> None:
    """Log a prominent update notification."""
    msg = (
        f"\n{'═' * 60}\n"
        f"  🔔 Crow Agent update available!\n"
        f"  Current: {__version__}  →  Latest: {latest}\n"
        f"  Run: cd ~/crow-agent && git pull && pip install -e .\n"
        f"{'═' * 60}"
    )
    logger.warning(msg)
