"""
SelfModel — Crow's self-awareness persistence layer.

SQLite-backed single-row JSON blob in the `self_state` table of the
sessions database. Components push partial updates via update(path, value).
Derived fields (mood, can_act) computed on read. One-way data flow.

Design decisions:
  - SQLite (not JSON file) — durability, same DB as turns.
  - Push-based updates — no polling.
  - Flat architecture: direct function calls, no closures, no layers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("crow_agent.self_model")

# ── Mood computation thresholds ──
_SHARP_REFLECT_SCORE = 4
_DEGRADED_REFLECT_SCORE = 2
_DEGRADED_ERROR_STREAK = 3
_DISK_SOFT_BLOCK_PCT = 95
_RAM_SOFT_BLOCK_PCT = 95
_ERROR_STREAK_BLOCK = 5

# ── Default snapshot (fresh state) ──
_DEFAULT_STATE: dict[str, Any] = {
    "identity": {
        "model_name": "unknown",
        "provider": "unknown",
        "context_window": 0,
        "project_root": "",
        "version": "",
        "uptime_seconds": 0,
        "deployed_at": "",
    },
    "status": {
        "health": {
            "disk_pct": 0,
            "ram_pct": 0,
            "cpu_load": 0.0,
            "last_check": "",
            "errors_streak": 0,
        },
        "sessions": {
            "active_chats": 0,
            "turns_today": 0,
            "last_user_activity": 0.0,
            "user_online": False,
        },
        "initiatives": {
            "active": 0,
            "paused": False,
            "total_today": 0,
            "last_result": "none",
        },
    },
    "reflection": {
        "last_reflect_time": "",
        "last_insight": "",
        "last_insight_score": 0,
        "self_assessment": "",
        "quality_focus": "",
    },
    "context": {
        "current_task": "",
        "active_conversation_summary": "",
        "agenda_today": [],
        "pending_fixes": [],
    },
    "memory": {
        "turns_stored": 0,
        "vault_pages": 0,
        "tools_loaded": 0,
    },
    "heartbeat": {
        "running": False,
        "slices_active": 0,
        "tick_interval_seconds": 0,
        "last_tick": "",
    },
    "extensions": {
        "loaded": [],
    },
    "connections": {
        "cron": "unknown",
        "git": "unknown",
    },
    "mood": "normal",
}


class SelfModel:
    """Persistent self-awareness store for Crow.

    Single-row JSON blob in the `self_state` table of the sessions database.
    Components push partial updates via update(path, value). Read via
    snapshot(). Derived fields (mood, can_act) computed on read.

    Thread-safe: all writes go through a lock.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._start_time = time.time()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS self_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            self._conn.execute("""
                INSERT OR IGNORE INTO self_state (id, state_json)
                VALUES (1, ?)
            """, (json.dumps(_DEFAULT_STATE),))
            self._migrate_state()
            self._conn.commit()

    def _migrate_state(self) -> None:
        """Ensure existing state has all keys from the current default schema."""
        row = self._conn.execute(
            "SELECT state_json FROM self_state WHERE id = 1"
        ).fetchone()
        if not row:
            return
        current = json.loads(row["state_json"])
        changed = False
        for key, default_val in _DEFAULT_STATE.items():
            if key not in current:
                current[key] = default_val
                changed = True
        if changed:
            self._conn.execute(
                "UPDATE self_state SET state_json = ?, updated_at = datetime('now') WHERE id = 1",
                (json.dumps(current),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── Write ──────────────────────────────────────────────────────────

    def update(self, path: str, value: Any) -> None:
        """Push a partial update into the self-model.

        Args:
            path: Dot-separated path, e.g. "status.health" or "identity".
            value: Dict of key-values to merge at that path, or a scalar
                   to replace the leaf.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT state_json FROM self_state WHERE id = 1"
            ).fetchone()
            state = json.loads(row["state_json"]) if row else dict(_DEFAULT_STATE)

            keys = path.split(".")
            target = state
            for key in keys[:-1]:
                if key not in target or not isinstance(target[key], dict):
                    target[key] = {}
                target = target[key]
            leaf_key = keys[-1]
            if isinstance(value, dict):
                if leaf_key not in target or not isinstance(target[leaf_key], dict):
                    target[leaf_key] = {}
                target[leaf_key].update(value)
            else:
                target[leaf_key] = value

            self._conn.execute(
                "UPDATE self_state SET state_json = ?, updated_at = datetime('now') WHERE id = 1",
                (json.dumps(state),),
            )
            self._conn.commit()

    # ── Read ───────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Return the full self-model state as a dict."""
        with self._lock:
            row = self._conn.execute(
                "SELECT state_json FROM self_state WHERE id = 1"
            ).fetchone()
            if not row:
                return dict(_DEFAULT_STATE)
            state = json.loads(row["state_json"])
            state["identity"]["uptime_seconds"] = int(time.time() - self._start_time)
            state["mood"] = self._compute_mood(state)
            return state

    # ── Derived ────────────────────────────────────────────────────────

    def mood(self) -> str:
        """Compute mood: 'sharp', 'normal', or 'degraded'.

        Derived from reflect scores, error streaks, and initiative results.
        """
        return self._compute_mood(self.snapshot())

    @staticmethod
    def _compute_mood(state: dict[str, Any]) -> str:
        degraded = False
        health = state.get("status", {}).get("health", {})
        reflect = state.get("reflection", {})
        initiatives = state.get("status", {}).get("initiatives", {})

        if health.get("errors_streak", 0) >= _DEGRADED_ERROR_STREAK:
            degraded = True
        if reflect.get("last_insight_score", 0) <= _DEGRADED_REFLECT_SCORE and reflect.get("last_insight_score", 0) > 0:
            degraded = True
        if initiatives.get("last_result") == "failure":
            degraded = True

        if degraded:
            return "degraded"

        if (
            reflect.get("last_insight_score", 0) >= _SHARP_REFLECT_SCORE
            and health.get("errors_streak", 0) == 0
            and initiatives.get("last_result") == "success"
        ):
            return "sharp"

        return "normal"

    def can_act(self) -> bool | str:
        """Check if Crow is healthy enough for autonomous action.

        Returns:
            True if healthy.
            A reason string if action should be soft-blocked.
        """
        state = self.snapshot()
        health = state.get("status", {}).get("health", {})

        if health.get("disk_pct", 0) >= _DISK_SOFT_BLOCK_PCT:
            return f"Disk at {health['disk_pct']}% — too full for file writes"
        if health.get("ram_pct", 0) >= _RAM_SOFT_BLOCK_PCT:
            return f"RAM at {health['ram_pct']}% — too low for safe operation"
        if health.get("errors_streak", 0) >= _ERROR_STREAK_BLOCK:
            return f"Error streak {health['errors_streak']} — investigate before acting"

        return True

    # ── Formatting ─────────────────────────────────────────────────────

    def to_prompt_chunk(self) -> str:
        """Format a compact self-status block for system prompt injection."""
        s = self.snapshot()
        ident = s["identity"]
        health = s["status"]["health"]
        sessions = s["status"]["sessions"]
        initiatives = s["status"]["initiatives"]
        reflect = s["reflection"]
        mood_val = s["mood"]

        lines = ["## Self Status"]

        # Identity line
        lines.append(
            f"{ident['model_name']} | {ident['provider']} | "
            f"{ident['context_window'] // 1000}K ctx | "
            f"Uptime {ident['uptime_seconds'] // 60}m"
        )

        # Health line
        health_parts = [f"Disk {health['disk_pct']}%", f"RAM {health['ram_pct']}%"]
        if health.get("errors_streak", 0) > 0:
            health_parts.append(f"Errors {health['errors_streak']}")
        lines.append(" | ".join(health_parts))

        # Activity line
        act_parts = [f"{initiatives.get('active', 0)} initiatives"]
        if sessions.get("user_online"):
            last_act = sessions.get("last_user_activity", 0)
            mins_ago = int((time.time() - last_act) / 60) if last_act else 99
            act_parts.append(f"User {mins_ago}m ago")

        lines.append(" | ".join(act_parts))

        # Mood (only show if not normal)
        if mood_val != "normal":
            lines.append(f"Mood: {mood_val}")

        # Reflect insight (if recent and scored)
        if reflect.get("last_insight") and reflect.get("last_insight_score", 0) >= 3:
            lines.append(f'Reflect: "{reflect["last_insight"][:100]}" ({reflect["last_insight_score"]}/5)')

        # Current task
        if s["context"].get("current_task"):
            lines.append(f"Current: {s['context']['current_task']}")

        # Temporal presence: what the user was last doing
        conv = s["context"].get("active_conversation_summary", "")
        if conv and sessions.get("user_online"):
            lines.append(f"Last exchange: {conv[:150]}")

        return "\n".join(lines)
