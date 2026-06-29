"""SQLite session database with FTS5 for historical conversation retrieval."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3

logger = logging.getLogger("crow_agent.state")
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path.home() / ".crow_agent" / "sessions.db"

_SQLITE_BUSY_MSG = "database is locked"
_SQLITE_BUSY_RETRIES = 3
_SQLITE_BUSY_DELAY = 0.1  # 100ms


def _retry_on_locked(fn, *args, **kwargs):
    """Call fn(*args, **kwargs), retrying on SQLite locked errors."""
    last_err = None
    for attempt in range(_SQLITE_BUSY_RETRIES):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if _SQLITE_BUSY_MSG in str(e).lower():
                last_err = e
                time.sleep(_SQLITE_BUSY_DELAY)
                continue
            raise
    raise last_err  # type: ignore[misc]


def _db_path() -> Path:
    """Resolve the database path from env or default."""
    return Path(os.environ.get("CROW_AGENT_DB_PATH", str(DEFAULT_DB_PATH)))




class _BatchContext:
    """Context manager that defers CrowState commits until exit."""

    def __init__(self, state: CrowState) -> None:
        self._state = state

    def __enter__(self) -> None:
        self._state._enter_batch()

    def __exit__(self, *exc: object) -> None:
        if exc[0] is not None:
            # Exception occurred — rollback + re-balance depth
            try:
                self._state._conn.rollback()
            except Exception:
                logger.warning("rollback failed after batch error", exc_info=True)
            self._state._exit_batch_no_commit()
            return
        self._state._exit_batch()


# ── Schema migrations (stamp-based) ──

# Current schema version. Bump when adding a migration entry below.
SCHEMA_VERSION = 3

# Each migration: (version, description, SQL string)
# Versions are applied sequentially. Never modify a released migration.
# Add new entries at the end only.
_MIGRATIONS: list[tuple[int, str, str]] = [
    # Version 1 is the initial schema — see _init_schema().
    (2, "add goal tracking columns to tasks",
     "ALTER TABLE tasks ADD COLUMN parent_id TEXT DEFAULT NULL;"
     "ALTER TABLE tasks ADD COLUMN progress INTEGER DEFAULT 0;"
     "ALTER TABLE tasks ADD COLUMN target_value TEXT DEFAULT NULL;"),
    (3, "add parent_session_id for compression lineage tracking",
     "ALTER TABLE sessions ADD COLUMN parent_session_id TEXT DEFAULT NULL;"),
]


def _migrate(conn: sqlite3.Connection, lock: threading.RLock) -> None:
    """Stamp-based migration runner.

    Checks current schema version from _schema_version table,
    applies any pending migrations in order, stamps each on success.
    Rolls back on failure — never leave the DB in a half-migrated state.
    """
    with lock:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _schema_version ("
            "  version INTEGER PRIMARY KEY,"
            "  description TEXT NOT NULL,"
            "  applied_at TEXT DEFAULT (datetime('now'))"
            ")"
        )
        conn.commit()

        row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
        current_ver = row[0] if row and row[0] else 0

        for ver, desc, sql in _MIGRATIONS:
            if ver <= current_ver:
                continue
            try:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO _schema_version (version, description) VALUES (?, ?)",
                    (ver, desc),
                )
                conn.commit()
                logger.info("Schema migration v%d: %s", ver, desc)
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e).lower():
                    # Column already exists — mark as applied and continue
                    conn.rollback()
                    conn.execute(
                        "INSERT OR IGNORE INTO _schema_version (version, description) VALUES (?, ?)",
                        (ver, desc),
                    )
                    conn.commit()
                    logger.info("Schema migration v%d: %s (already applied)", ver, desc)
                else:
                    raise
            except Exception:
                conn.rollback()
                logger.exception("Schema migration v%d FAILED: %s", ver, desc)
                raise

        logger.debug("Schema at version %d", current_ver)


class CrowState:
    """Manages session persistence and FTS5-backed conversation search."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._path = Path(db_path) if db_path else _db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA wal_autocheckpoint = 128")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA cache_size = -64000")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA mmap_size = 268435456")
        self._lock = threading.RLock()
        self._batch_depth = 0  # >0 means inside batch()
        self._init_schema()

    def _init_schema(self) -> None:
        """Create tables and FTS5 virtual table if they don't exist."""
        with self._lock:
            self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
                content TEXT NOT NULL DEFAULT '',
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                content,
                session_id UNINDEXED,
                turn_id UNINDEXED,
                content='turns',
                content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
                INSERT INTO turns_fts(rowid, content, session_id, turn_id)
                VALUES (new.id, new.content, new.session_id, new.id);
            END;

            CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
                INSERT INTO turns_fts(turns_fts, rowid, content, session_id, turn_id)
                VALUES ('delete', old.id, old.content, old.session_id, old.id);
            END;

            CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, id);

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                deadline TEXT,
                priority TEXT DEFAULT 'medium' CHECK(priority IN ('low','medium','high')),
                tags TEXT DEFAULT '[]',
                status TEXT DEFAULT 'pending' CHECK(status IN ('pending','in_progress','done','cancelled')),
                repeat TEXT,
                snoozed_until TEXT,
                parent_id TEXT DEFAULT NULL,
                progress INTEGER DEFAULT 0,
                target_value TEXT DEFAULT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)
            # Add token columns if missing (existing databases)
            for col in ("prompt_tokens", "completion_tokens"):
                try:
                    self._conn.execute(f"ALTER TABLE turns ADD COLUMN {col} INTEGER DEFAULT 0")
                except sqlite3.OperationalError:
                    pass

            # Tool output storage for compression
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS tool_outputs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    output TEXT NOT NULL,
                    arguments TEXT NOT NULL DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)

            # Separate FTS5 index on tool outputs — manually synced in store_tool_output.
            # Independent table (not content-sync) because tool_outputs uses TEXT PK.
            self._conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS tool_outputs_fts USING fts5(
                    output, arguments, tool_name, oid UNINDEXED,
                    tokenize='porter unicode61'
                )
            """)
            self._maybe_commit()

            # Add arguments column if missing (existing DBs from before 2026-06-15)
            try:
                self._conn.execute(
                    "ALTER TABLE tool_outputs ADD COLUMN arguments TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists

            # ponytail: turn_id for direct turn-to-tool joins (2026-06-21)
            try:
                self._conn.execute(
                    "ALTER TABLE tool_outputs ADD COLUMN turn_id INTEGER REFERENCES turns(id)"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Clean up tool outputs older than 30 days (TTL-evict)
        self._conn.execute(
            "DELETE FROM tool_outputs WHERE created_at < datetime('now', '-30 days')"
        )
        self._maybe_commit()

        # Clean up turns older than 90 days
        self._conn.execute(
            "DELETE FROM turns WHERE created_at < datetime('now', '-90 days')"
        )
        self._maybe_commit()

        # Run stamp-based migration (applies any pending _MIGRATIONS entries)
        _migrate(self._conn, self._lock)

        # turn_metrics — pre-dates migration system. Kept here for backward compat.
        # Future table additions should go in _MIGRATIONS instead.
        try:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS turn_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_count INTEGER DEFAULT 0,
                    phase TEXT NOT NULL,
                    duration_ms INTEGER DEFAULT 0,
                    tool_name TEXT DEFAULT NULL,
                    provider TEXT DEFAULT NULL,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    failure INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            self._maybe_commit()
        except Exception:
            logger.warning("tool_outputs table init failed", exc_info=True)

    def create_session(self, session_id: str, parent_session_id: str | None = None) -> None:
        """Insert a session row, ignoring if it already exists."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions (id, parent_session_id) VALUES (?, ?)",
                (session_id, parent_session_id),
            )
            self._maybe_commit()

    def append_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> int:
        """Append a conversation turn with optional token usage. Returns rowid."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO turns (session_id, role, content, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, prompt_tokens, completion_tokens),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at = datetime('now') WHERE id = ?",
                (session_id,),
            )
            self._maybe_commit()
            return cur.lastrowid

    # ── batch commit context manager ──

    def batch(self) -> _BatchContext:
        """Context manager that defers commits until exit."""
        return _BatchContext(self)

    def _enter_batch(self) -> None:
        with self._lock:
            self._batch_depth += 1

    def _exit_batch(self) -> None:
        with self._lock:
            self._batch_depth -= 1
            if not self._batch_depth:
                self._conn.commit()

    def _exit_batch_no_commit(self) -> None:
        """Decrement batch depth without committing (rollback path)."""
        with self._lock:
            self._batch_depth -= 1

    def _maybe_commit(self) -> None:
        """Commit only if not inside a batch context."""
        with self._lock:
            if not self._batch_depth:
                self._conn.commit()

    def history(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent turns for a session, oldest first."""
        with self._lock:
            rows = self._conn.execute(
            "SELECT role, content, prompt_tokens, completion_tokens FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def token_totals(self, session_id: str) -> dict[str, int]:
        """Return total prompt and completion tokens for a session."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0) AS prompt, COALESCE(SUM(completion_tokens),0) AS completion FROM turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return {"prompt": row["prompt"], "completion": row["completion"], "total": row["prompt"] + row["completion"]}

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """FTS5 search across turns + tool outputs with BM25 recency weighting.

        Uses bm25() * (1 + days_old * 0.05) so older turns get a small penalty.
        Stop-word filter + OR for better recall. (Phase 4)
        """
        import re
        words = re.findall(r"[a-zA-Z0-9]{2,}", query)
        if not words:
            return []
        _STOP_WORDS = frozenset({
            'the','and','for','are','but','not','you','all','can','had',
            'her','was','one','our','out','has','have','been','its',
            'now','before','about','from','that','your','good','task',
            'hours','already','gave','with','what','when','where','how',
            'this','that','these','those','will','would','could','should',
            'did','does','done','doing','got','get','tell','show','make',
            'just','like','also','very','well','back','over','into','than',
            'then','them','some','such','only','more','much','many','here',
            'there','say','says','said','see','seen','know','need','want',
            'may','might','shall','can','could','would','should','must',
        })
        important = [w for w in words if len(w) > 2 and w.lower() not in _STOP_WORDS]
        fts_query = " OR ".join(important) if important else words[0]

        results: list[dict[str, Any]] = []
        with self._lock:
            try:
                rows = self._conn.execute("""
                    SELECT t.session_id, t.role, t.content, t.created_at,
                           'turn' AS source, NULL AS tool_name, NULL AS oid,
                           bm25(turns_fts, 10.0, 5.0) * (
                               1.0 + (julianday('now') - julianday(t.created_at)) * 0.05
                           ) AS weighted_rank
                    FROM turns_fts f JOIN turns t ON t.id = f.rowid
                    WHERE turns_fts MATCH ? ORDER BY weighted_rank LIMIT ?
                """, (fts_query, limit)).fetchall()
                results.extend(dict(r) for r in rows)
            except Exception:
                logger.debug("turns_fts search failed")
            try:
                rows = self._conn.execute("""
                    SELECT o.session_id, 'tool' AS role, o.output AS content,
                           o.created_at, 'tool_output' AS source,
                           o.tool_name, o.arguments,
                           bm25(tool_outputs_fts, 10.0, 5.0) * (
                               1.0 + (julianday('now') - julianday(o.created_at)) * 0.05
                           ) AS weighted_rank
                    FROM tool_outputs_fts f JOIN tool_outputs o ON o.id = f.oid
                    WHERE tool_outputs_fts MATCH ? ORDER BY weighted_rank LIMIT ?
                """, (fts_query, limit)).fetchall()
                results.extend(dict(r) for r in rows)
            except Exception:
                logger.debug("tool_outputs_fts search failed")
        results.sort(key=lambda r: r.get("weighted_rank", 999))
        return results[:limit]


    def create_goal(self, title: str, target_value: str = "", description: str = "") -> str:
        """Create a persistent goal. Returns goal ID."""
        return self.create_task(
            title=title,
            description=description,
            priority="high",
            tags=["goal"],
        )

    def update_progress(self, goal_id: str, progress: int, target_value: str | None = None) -> bool:
        """Update goal progress (0-100) and optionally target_value."""
        fields = {"progress": max(0, min(100, progress))}
        if target_value is not None:
            fields["target_value"] = target_value
        return self.update_task(goal_id, **fields)

    def list_goals(self) -> list[dict[str, Any]]:
        """List all goals with their progress."""
        tasks = self.list_tasks(tag="goal")
        # Filter: goals are tasks tagged 'goal' and not done/cancelled
        return [t for t in tasks if t.get("status") not in ("done", "cancelled")]

    def add_subtask(self, goal_id: str, title: str, description: str = "") -> str:
        """Add a subtask to a goal. Returns subtask ID."""
        return self.create_task(
            title=title,
            description=description,
            tags=["goal", "subtask"],
        )

    def prune_worker_sessions(self, max_age_days: int = 7) -> int:
        """Delete worker sessions older than max_age_days. Returns count removed."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE id LIKE 'worker:%' "
                "AND updated_at < datetime('now', ?)",
                (f'-{max_age_days} days',),
            )
            count = cur.rowcount
            if count:
                self._maybe_commit()
            return count

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def fork_session(self, source_id: str, new_id: str, parent_session_id: str | None = None) -> bool:
        """Fork a session: copy all turns from source to new session.
        Returns False if new_id exists. Sets parent_session_id if provided."""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (new_id,)
            ).fetchone()
            if exists:
                return False
            self._conn.execute(
                "INSERT INTO sessions (id, parent_session_id) VALUES (?, ?)",
                (new_id, parent_session_id or source_id),
            )
            self._conn.execute(
                "INSERT INTO turns (session_id, role, content, prompt_tokens, completion_tokens) "
                "SELECT ?, role, content, prompt_tokens, completion_tokens "
                "FROM turns WHERE session_id = ? ORDER BY id",
                (new_id, source_id),
            )
            self._maybe_commit()
            return True

    def rename_session(self, old_id: str, new_id: str) -> bool:
        """Rename a session. Returns False if new_id already exists."""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (new_id,)
            ).fetchone()
            if exists:
                return False
            self._conn.execute(
                "UPDATE sessions SET id = ? WHERE id = ?", (new_id, old_id)
            )
            self._conn.execute(
                "UPDATE turns SET session_id = ? WHERE session_id = ?", (new_id, old_id)
            )
            self._maybe_commit()
            return True

    def store_tool_output(self, session_id: str, tool_name: str, output: str, arguments: str = "", turn_id: int | None = None) -> str:
        """Store full tool output and return a short retrieval ID.

        Also indexes into tool_outputs_fts for RECALL-phase search.
        Args: the JSON string of tool call arguments (contains function params).
        turn_id: optional FK to turns.id for direct correlation.
        """
        with self._lock:
            oid = "o_" + secrets.token_hex(4)
            self._conn.execute(
                "INSERT INTO tool_outputs (id, session_id, tool_name, output, arguments, turn_id) VALUES (?, ?, ?, ?, ?, ?)",
                (oid, session_id, tool_name, output, arguments, turn_id),
            )
            # Sync to FTS5 for RECALL search (independent FTS5 table, auto rowid)
            try:
                self._conn.execute(
                    "INSERT INTO tool_outputs_fts (output, arguments, tool_name, oid) VALUES (?, ?, ?, ?)",
                    (output, arguments, tool_name, oid),
                )
            except Exception:
                logger.debug("FTS5 sync failed for tool_output %s", oid)
            self._maybe_commit()
            return oid

    def backfill_turn_id(self, session_id: str, turn_id: int) -> None:
        """Set turn_id on tool_outputs that were written before the turn ID was known."""
        with self._lock:
            self._conn.execute(
                "UPDATE tool_outputs SET turn_id = ? WHERE session_id = ? AND turn_id IS NULL",
                (turn_id, session_id),
            )
            self._maybe_commit()

    def get_tool_output(self, output_id: str) -> str | None:
        """Retrieve full tool output by ID. Returns None if not found or expired."""
        with self._lock:
            row = self._conn.execute(
                "SELECT output FROM tool_outputs WHERE id = ?", (output_id,)
            ).fetchone()
        return row["output"] if row else None

    def record_turn_metric(
        self,
        session_id: str,
        turn_count: int,
        phase: str,
        duration_ms: int,
        tool_name: str | None = None,
        provider: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        failure: bool = False,
    ) -> None:
        """Append a per-phase timing record. Non-blocking on error."""
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO turn_metrics
                       (session_id, turn_count, phase, duration_ms, tool_name,
                        provider, prompt_tokens, completion_tokens, failure)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id, turn_count, phase, duration_ms,
                        tool_name, provider, prompt_tokens, completion_tokens,
                        1 if failure else 0,
                    ),
                )
                self._maybe_commit()
        except Exception:
            logger.exception("Failed to record turn metric (non-blocking)")

    def delete_session(self, session_id: str) -> None:
        """Delete a session and all its turns (FTS trigger cleans up index)."""
        with self._lock:
            self._conn.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._maybe_commit()

    # ── task management ──

    @staticmethod
    def compute_next_deadline(deadline: str, repeat: str) -> str | None:
        """Compute the next deadline for a recurring task.

        Supported repeat patterns:
          - "weekly"     → add 7 days, preserve time
          - "daily"      → add 1 day, preserve time
          - "weekdays"   → skip to next Mon–Fri
          - "friday"     → next Friday (weekly_friday)

        Returns ISO datetime string, or None if pattern not recognised.
        """
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        try:
            dt = datetime.fromisoformat(deadline)
            if not isinstance(dt, datetime):
                dt = datetime(dt.year, dt.month, dt.day)
        except (ValueError, TypeError):
            return None

        # If deadline is naive, interpret as configured timezone (CROWD_TZ, default UTC)
        if dt.tzinfo is None:
            import os
            tz_name = os.environ.get("CROWD_TZ", "UTC")
            dt = dt.replace(tzinfo=ZoneInfo(tz_name))

        pattern = repeat.strip().lower()

        if pattern == "daily":
            next_dt = dt + timedelta(days=1)
        elif pattern == "weekly":
            next_dt = dt + timedelta(weeks=1)
        elif pattern == "weekdays":
            next_dt = dt + timedelta(days=1)
            while next_dt.weekday() >= 5:  # Sat=5, Sun=6
                next_dt += timedelta(days=1)
        elif pattern == "friday":
            # Advance to next Friday
            days_ahead = (4 - dt.weekday()) % 7  # 4 = Friday
            if days_ahead == 0:
                days_ahead = 7  # already Friday → next Friday
            next_dt = dt + timedelta(days=days_ahead)
        else:
            return None  # unknown pattern

        return next_dt.isoformat()

    def create_task(
        self,
        title: str,
        description: str = "",
        deadline: str | None = None,
        priority: str = "medium",
        tags: list[str] | None = None,
        repeat: str | None = None,
    ) -> str:
        """Create a task. Returns the task ID."""
        task_id = "t_" + secrets.token_hex(8)
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (id, title, description, deadline, priority, tags, repeat) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, title, description, deadline, priority, json.dumps(tags or []), repeat),
            )
            self._maybe_commit()
        return task_id

    def advance_recurring_task(self, task_id: str) -> str | None:
        """Atomically advance a recurring task: mark done, create next occurrence.

        Uses self._lock to prevent race conditions when multiple callers
        (e.g. _tick + complete_task) try to advance the same task.

        Returns the new task ID if a next occurrence was created,
        or None if the task was already done, has no repeat pattern,
        or cannot compute a next deadline.
        """
        with self._lock:
            task = self.get_task(task_id)
            if not task or task["status"] != "pending":
                return None
            repeat = task.get("repeat")
            deadline = task.get("deadline")
            if not repeat or not deadline:
                return None

            next_deadline = self.compute_next_deadline(deadline, repeat)
            if not next_deadline:
                return None

            self.update_task(task_id, status="done", snoozed_until=None)
            tags_raw = task.get("tags", [])
            if isinstance(tags_raw, str):
                tags_raw = json.loads(tags_raw)
            new_id = self.create_task(
                title=task["title"],
                description=task.get("description", ""),
                deadline=next_deadline,
                priority=task.get("priority", "medium"),
                tags=tags_raw,
                repeat=repeat,
            )
            return new_id

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Get a single task by ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["tags"] = json.loads(d.get("tags", "[]"))
        return d

    def list_tasks(self, status: str | None = None, tag: str | None = None) -> list[dict[str, Any]]:
        """List tasks, optionally filtered by status or tag."""
        where = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY deadline IS NULL, deadline ASC, created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d.get("tags", "[]"))
            if tag and tag not in d["tags"]:
                continue
            results.append(d)
        return results

    def update_task(self, task_id: str, **fields: Any) -> bool:
        """Update task fields by ID. Returns False if task not found."""
        allowed = {"title", "description", "deadline", "priority", "tags", "status", "repeat", "snoozed_until", "progress", "target_value"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        if "tags" in updates and isinstance(updates["tags"], list):
            updates["tags"] = json.dumps(updates["tags"])
        updates["updated_at"] = None  # triggers default
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE tasks SET {set_clause} WHERE id = ?",
                values + [task_id],
            )
            self._maybe_commit()
        return cur.rowcount > 0

    def delete_task(self, task_id: str) -> bool:
        """Delete a task by ID. Returns False if not found."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._maybe_commit()
        return cur.rowcount > 0
