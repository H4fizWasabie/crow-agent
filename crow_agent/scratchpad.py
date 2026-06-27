"""Scratchpad — SQLite-backed crew task tracker.

Each crew run gets tasks. Workers write summaries. Monitor reads for stall detection.
Logs archived for 30 days for pattern learning via embeddings.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("crow_agent.scratchpad")


class CrewScratchpadDB:
    """SQLite table for crew task tracking.

    Schema:
        CREATE TABLE crew_tasks (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            worker TEXT NOT NULL,
            status TEXT NOT NULL,   -- pending | running | done | failed
            summary TEXT DEFAULT '',
            ts REAL NOT NULL        -- unix timestamp
        )
        CREATE INDEX idx_crew_tasks_run ON crew_tasks(run_id, status)

    API:
        write_task(run_id, task_id, worker, status, summary)
        get_active(run_id) → list of pending/running tasks
        get_log(run_id) → all tasks for archive
        archive_run(run_id) → move to crew_logs/ markdown
        search_similar(summary) → embedding similarity (future)
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS crew_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                worker TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                summary TEXT DEFAULT '',
                ts REAL NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_crew_tasks_run
            ON crew_tasks(run_id, status)
        """)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def write_task(
        self, run_id: str, task_id: str, worker: str, status: str, summary: str = ""
    ) -> None:
        """Upsert a task entry. Latest write wins for same run_id+task_id."""
        now = time.time()
        existing = self._conn.execute(
            "SELECT id FROM crew_tasks WHERE run_id = ? AND task_id = ?",
            (run_id, task_id),
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE crew_tasks SET worker=?, status=?, summary=?, ts=? WHERE run_id=? AND task_id=?",
                (worker, status, summary, now, run_id, task_id),
            )
        else:
            self._conn.execute(
                "INSERT INTO crew_tasks (run_id, task_id, worker, status, summary, ts) VALUES (?,?,?,?,?,?)",
                (run_id, task_id, worker, status, summary, now),
            )
        self._conn.commit()

    def get_active(self, run_id: str) -> list[dict[str, Any]]:
        """Get pending and running tasks for a run."""
        rows = self._conn.execute(
            "SELECT * FROM crew_tasks WHERE run_id = ? AND status IN ('pending', 'running') ORDER BY ts",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_active(self) -> list[dict[str, Any]]:
        """Get active tasks across all runs."""
        rows = self._conn.execute(
            "SELECT * FROM crew_tasks WHERE status IN ('pending', 'running') ORDER BY ts"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_log(self, run_id: str) -> list[dict[str, Any]]:
        """Get all tasks for a run (including done/failed)."""
        rows = self._conn.execute(
            "SELECT * FROM crew_tasks WHERE run_id = ? ORDER BY ts",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stale(self, run_id: str, threshold_seconds: float = 90) -> list[dict[str, Any]]:
        """Get active tasks not updated in threshold_seconds."""
        cutoff = time.time() - threshold_seconds
        rows = self._conn.execute(
            "SELECT * FROM crew_tasks WHERE run_id = ? AND status IN ('pending', 'running') AND ts < ?",
            (run_id, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]

    def archive_run(self, run_id: str) -> str:
        """Export a run log as markdown and delete from DB."""
        rows = self.get_log(run_id)
        if not rows:
            return ""
        lines = [f"# Crew Run: {run_id}", "", f"Archived: {time.strftime('%Y-%m-%d %H:%M')}", ""]
        for r in rows:
            status_icon = {"done": "✅", "failed": "❌", "running": "🔄", "pending": "⏳"}
            icon = status_icon.get(r["status"], "❓")
            lines.append(f"## {icon} {r['worker']} — {r['task_id']} ({r['status']})")
            lines.append(f"{r['summary']}")
            lines.append("")
        text = "\n".join(lines)
        log_dir = Path.home() / ".crow_agent" / "crew_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{run_id}.md"
        log_path.write_text(text)
        self._conn.execute("DELETE FROM crew_tasks WHERE run_id = ?", (run_id,))
        self._conn.commit()
        logger.info("Archived crew run: %s → %s", run_id, log_path)
        return str(log_path)
