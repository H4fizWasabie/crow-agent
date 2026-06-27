"""Lightweight error recurrence tracker for failure-prone agent code.

Tracks error signatures across turns. After N recurrences, signals
caller to warn or escalate (abort + notify user).
"""

import time
import logging
from typing import Any

logger = logging.getLogger("crow_agent")

WARN_THRESHOLD = 3
ESCALATE_THRESHOLD = 5


class ErrorTracker:
    """Tracks recurring error signatures across turns."""

    def __init__(self) -> None:
        self._errors: dict[str, dict[str, Any]] = {}

    def record(self, signature: str, context: str = "") -> dict[str, Any]:
        now = time.time()
        if signature not in self._errors:
            self._errors[signature] = {
                "count": 0, "first": now, "last": now, "contexts": [],
            }
        err = self._errors[signature]
        err["count"] += 1
        err["last"] = now
        if context and (not err["contexts"] or err["contexts"][-1] != context):
            err["contexts"].append(context)

        count = err["count"]
        result = {"count": count, "warn": count >= WARN_THRESHOLD,
                  "escalate": count >= ESCALATE_THRESHOLD}

        if result["escalate"]:
            logger.error("ErrorTracker [%s] recurred %d times — ESCALATING", signature, count)
        elif result["warn"]:
            logger.warning("ErrorTracker [%s] recurred %d times", signature, count)
        return result

    def reset(self, signature: str | None = None) -> None:
        if signature:
            self._errors.pop(signature, None)
        else:
            self._errors.clear()

    def get_count(self, signature: str) -> int:
        err = self._errors.get(signature)
        return err["count"] if err else 0


_default_tracker: ErrorTracker | None = None


def get_error_tracker() -> ErrorTracker:
    global _default_tracker
    if _default_tracker is None:
        _default_tracker = ErrorTracker()
    return _default_tracker
