"""Update checker — notifies user when a newer version is available on GitHub.

Non-blocking, cache-friendly, zero-dependency. Fires on app startup.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from urllib.request import urlopen, Request

logger = logging.getLogger("crow_agent.update")

_GITHUB_RELEASES_URL = (
    "https://api.github.com/repos/H4fizWasabie/crow-agent/commits/main"
)
_CACHE_PATH = Path.home() / ".crow_agent" / "update_check_cache.json"
_CHECK_INTERVAL = 3600 * 6  # check every 6 hours


def check_for_updates() -> str | None:
    """Check GitHub for newer commits. Returns latest commit SHA if newer, None otherwise.

    Compares the latest main commit SHA against a cached SHA. If different,
    the user is running an outdated version. Cached every 6 hours.
    Never crashes — all failures are logged and swallowed.
    """
    now = time.time()

    if _CACHE_PATH.exists():
        try:
            cache = json.loads(_CACHE_PATH.read_text())
            if now - cache.get("checked_at", 0) < _CHECK_INTERVAL:
                return None  # already checked recently
        except (json.JSONDecodeError, OSError):
            pass

    try:
        req = Request(
            _GITHUB_RELEASES_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "crow-agent"},
        )
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            latest_sha = data.get("sha", "")
            latest_msg = data.get("commit", {}).get("message", "")[:80]
    except Exception as e:
        logger.debug("Update check failed (network/API): %s", e)
        return None

    # Compare against cached SHA
    prev_sha = ""
    if _CACHE_PATH.exists():
        try:
            prev_sha = json.loads(_CACHE_PATH.read_text()).get("sha", "")
        except (json.JSONDecodeError, OSError):
            pass

    is_new = prev_sha and latest_sha and latest_sha != prev_sha

    # Cache result
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps({"checked_at": now, "sha": latest_sha}))

    if is_new:
        _log_update_available(latest_msg, latest_sha[:8])
        return latest_sha

    return None


def _log_update_available(latest_msg: str, sha: str) -> None:
    """Log a prominent update notification."""
    msg = (
        f"\n{'═' * 60}\n"
        f"  🔔 Crow Agent update available! (commit {sha})\n"
        f"  Latest: {latest_msg}\n"
        f"  Run: cd ~/crow-agent && git pull\n"
        f"{'═' * 60}"
    )
    logger.warning(msg)
