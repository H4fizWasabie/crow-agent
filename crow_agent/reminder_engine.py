"""Reminder engine for task secretary. Async loop that checks tasks and fires Telegram reminders."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Configurable timezone — env CROWD_TZ (default UTC)
_CROWD_TZ_NAME = os.environ.get("CROWD_TZ", "UTC")
_TZ = ZoneInfo(_CROWD_TZ_NAME)

logger = logging.getLogger("crow_agent.reminder")

# Load holidays from user-configured file.
# Set CROWD_HOLIDAYS=/path/to/holidays.json in .env to enable holiday-aware reminders.
_HOLIDAYS_FILE = Path(os.environ.get("CROWD_HOLIDAYS", ""))
_HOLIDAYS: set[str] = set()
if _HOLIDAYS_FILE and _HOLIDAYS_FILE.exists():
    import json
    _HOLIDAYS = set(json.loads(_HOLIDAYS_FILE.read_text()).get("holidays", []))

# Weekends
_SATURDAY = 5
_SUNDAY = 6


_REMINDED_PATH = Path.home() / ".crow_agent" / "last_reminded.json"


class ReminderEngine:
    """Async loop that checks tasks and fires Telegram reminders.

    Flows:
      1h before deadline  → first reminder
      past deadline       → every 5 min if no reply
      snoozed             → skip until snooze expires
      holiday/weekend     → no reminders
    """

    def __init__(
        self,
        db: Any,
        send_fn: Any | None = None,
        resolution: float = 60.0,
        chat_id: int | None = None,
    ) -> None:
        self._db = db
        self._send_fn = send_fn  # async callable(chat_id, task_dict)
        self._resolution = resolution
        self._chat_id = chat_id
        self._task: asyncio.Task | None = None
        self._last_reminded: dict[str, float] = self._load_reminded()

    def _load_reminded(self) -> dict[str, float]:
        """Load reminded timestamps from disk."""
        try:
            if _REMINDED_PATH.exists():
                data = json.loads(_REMINDED_PATH.read_text())
                return {k: float(v) for k, v in data.items()}
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to load %s, starting fresh", _REMINDED_PATH)
        return {}

    def _save_reminded(self) -> None:
        """Persist reminded timestamps to disk."""
        try:
            _REMINDED_PATH.parent.mkdir(parents=True, exist_ok=True)
            _REMINDED_PATH.write_text(json.dumps(self._last_reminded), encoding="utf-8")
        except OSError:
            pass  # non-critical

    # ── lifecycle ──

    def start(self) -> None:
        """Start the reminder loop as a background asyncio task."""
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._loop())
        logger.info("Reminder engine started (resolution=%.1fs)", self._resolution)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("Reminder engine stopped")

    # ── core loop ──

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._resolution)
            try:
                await self._tick()
            except Exception:
                logger.exception("Reminder tick failed")

    async def _tick(self) -> None:
        """Check all non-done tasks and send reminders for those due."""
        if not self._send_fn or not self._chat_id:
            return

        # Skip reminders while user is actively chatting
        from crow_agent.heartbeat_engine import _heartbeat_active_turns
        if _heartbeat_active_turns > 0:
            return

        tasks = self._db.list_tasks()
        now = datetime.now(timezone.utc)

        for task in tasks:
            if task["status"] in ("done", "cancelled"):
                continue

            # Check snooze
            snoozed = task.get("snoozed_until")
            if snoozed:
                try:
                    if datetime.fromisoformat(snoozed) > now:
                        continue
                except (ValueError, TypeError):
                    pass  # bad format, treat as not snoozed

            # Check holiday (local timezone)
            local_now = now.astimezone(_TZ)
            today_str = local_now.strftime("%Y-%m-%d")
            if today_str in _HOLIDAYS or local_now.weekday() in (_SATURDAY, _SUNDAY):
                continue  # no reminders on holidays/weekends

            deadline = task.get("deadline")
            if not deadline:
                continue  # no deadline = just a note, no reminder

            try:
                deadline_dt = datetime.fromisoformat(deadline)
                if not isinstance(deadline_dt, datetime):
                    deadline_dt = datetime(deadline_dt.year, deadline_dt.month, deadline_dt.day)
                if deadline_dt.tzinfo is None:
                    # Treat naive deadlines as configured timezone
                    deadline_dt = deadline_dt.replace(tzinfo=_TZ)
            except (ValueError, TypeError):
                continue

            delta = (deadline_dt - now).total_seconds()
            last_reminded = self._last_reminded.get(task["id"], 0.0)

            since_last = now.timestamp() - last_reminded

            # Decision: should we remind?
            should_remind = False
            if delta < 0 and since_last >= 300:  # past deadline, every 5 min
                should_remind = True
            elif 0 <= delta <= 3600 and last_reminded == 0:  # within 1h, not yet reminded
                should_remind = True

            if should_remind:
                self._last_reminded[task["id"]] = now.timestamp()
                self._save_reminded()
                await self._send_fn(self._chat_id, task)

            # ── Recurring task: auto-create next occurrence if deadline has passed ──
            if delta < 0 and task.get("repeat") and task.get("deadline"):
                new_id = self._db.advance_recurring_task(task["id"])
                if new_id:
                    new_task = self._db.get_task(new_id)
                    nd = new_task.get("deadline", "?") if new_task else "?"
                    logger.info(
                        "Recurring task '%s' → next occurrence %s (id=%s)",
                        task["title"], nd, new_id,
                    )

    # ── public helpers ──

    @staticmethod
    def next_holiday(today: str | None = None) -> str | None:
        """Return the next holiday date after today, or None if no holidays configured."""
        from datetime import date
        now = date.fromisoformat(today) if today else date.today()
        for h in sorted(_HOLIDAYS):
            if h > now.isoformat():
                return h
        return None

    @staticmethod
    def is_holiday(d: str | None = None) -> bool:
        from datetime import date
        ds = d or date.today().isoformat()
        return ds in _HOLIDAYS
