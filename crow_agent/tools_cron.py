"""Cron job management tools: list, create, remove, pause, resume cron jobs.

Reads/writes the same schedule.json used by CronEngine so agent-managed jobs
persist across restarts and execute via the same runner.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


_SCHEDULE_PATH = Path.home() / ".crow_agent" / "schedule.json"


def _load_schedule() -> list[dict[str, Any]]:
    """Load jobs from schedule.json. Returns empty list on any error."""
    try:
        if not _SCHEDULE_PATH.exists():
            return []
        data = json.loads(_SCHEDULE_PATH.read_text(encoding="utf-8"))
        return data.get("jobs", [])
    except (json.JSONDecodeError, OSError):
        return []


def _save_schedule(jobs: list[dict[str, Any]]) -> str | None:
    """Save jobs to schedule.json. Returns error string or None on success."""
    try:
        _SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SCHEDULE_PATH.write_text(
            json.dumps({"jobs": jobs}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return None
    except OSError as exc:
        return str(exc)


def _describe_interval(seconds: int) -> str:
    """Human-readable interval from seconds."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def register_tools(registry: Any, **kwargs: Any) -> None:
    """Register cron management tools."""

    @registry.register(
        description="List all scheduled cron jobs with their status and interval."
    )
    def cron_list() -> str:
        jobs = _load_schedule()
        if not jobs:
            return "No cron jobs scheduled."
        lines = []
        for j in jobs:
            enabled = "✅" if j.get("enabled", True) else "⏸"
            interval = _describe_interval(j["interval_seconds"])
            last_run = j.get("last_run", 0)
            if last_run:
                ago = time.time() - last_run
                last = f"{ago:.0f}s ago" if ago < 3600 else f"{ago / 3600:.1f}h ago"
            else:
                last = "never"
            lines.append(f"{enabled} {j['id']} — every {interval} (last: {last})")
            lines.append(f"   {j['prompt'][:120]}")
        return "\n".join(lines)

    @registry.register(
        description="Create or update a cron job. Runs on the given interval. Use cron_list() to see existing jobs."
    )
    def cron_create(
        job_id: str,
        prompt: str,
        interval_seconds: int,
        enabled: bool = True,
    ) -> str:
        if interval_seconds < 10:
            return "Error: minimum interval is 10 seconds."
        jobs = _load_schedule()
        existing = [j for j in jobs if j["id"] == job_id]

        entry = {
            "id": job_id,
            "prompt": prompt,
            "interval_seconds": interval_seconds,
            "enabled": enabled,
            "last_run": 0.0,
            "last_result": "",
            "last_error": "",
            "consecutive_failures": 0,
        }

        if existing:
            # Update existing
            entry["last_run"] = existing[0].get("last_run", 0.0)
            jobs = [entry if j["id"] == job_id else j for j in jobs]
        else:
            jobs.append(entry)

        err = _save_schedule(jobs)
        if err:
            return f"Error saving schedule: {err}"
        action = "Updated" if existing else "Created"
        return f"{action} cron job '{job_id}'. Runs every {_describe_interval(interval_seconds)}."

    @registry.register(
        description="Remove a cron job by ID."
    )
    def cron_remove(job_id: str) -> str:
        jobs = _load_schedule()
        before = len(jobs)
        jobs = [j for j in jobs if j["id"] != job_id]
        if len(jobs) == before:
            return f"Cron job '{job_id}' not found."
        err = _save_schedule(jobs)
        if err:
            return f"Error saving schedule: {err}"
        return f"Removed cron job '{job_id}'."

    @registry.register(
        description="Pause a cron job without removing it."
    )
    def cron_pause(job_id: str) -> str:
        jobs = _load_schedule()
        found = False
        for j in jobs:
            if j["id"] == job_id:
                j["enabled"] = False
                found = True
                break
        if not found:
            return f"Cron job '{job_id}' not found."
        err = _save_schedule(jobs)
        if err:
            return f"Error saving schedule: {err}"
        return f"Paused cron job '{job_id}'."

    @registry.register(
        description="Resume a paused cron job."
    )
    def cron_resume(job_id: str) -> str:
        jobs = _load_schedule()
        found = False
        for j in jobs:
            if j["id"] == job_id:
                j["enabled"] = True
                found = True
                break
        if not found:
            return f"Cron job '{job_id}' not found."
        err = _save_schedule(jobs)
        if err:
            return f"Error saving schedule: {err}"
        return f"Resumed cron job '{job_id}'."
