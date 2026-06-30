"""HeartbeatEngine — autonomous idle awareness for Crow."""


from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .crow_state import CrowState

logger = logging.getLogger("crow_agent.heartbeat")

# Shared with run_agent — set by AIAgent.run()/run_stream() entry/exit
_heartbeat_lock = threading.Lock()
_heartbeat_active_turns: int = 0
_heartbeat_last_user_interaction: float = 0.0


# Module-level callback — set by HeartbeatEngine.__init__ so mark_user_active
# can reset initiative pause counters (ADR-0005).
_reset_initiative_pause: Callable[[], None] | None = None


def mark_user_active() -> None:
    global _heartbeat_active_turns, _heartbeat_last_user_interaction
    with _heartbeat_lock:
        _heartbeat_active_turns += 1
        _heartbeat_last_user_interaction = time.time()
    # Reset initiative pause — user is back (ADR-0005)
    _cb = _reset_initiative_pause
    if _cb:
        _cb()


def mark_user_inactive() -> None:
    global _heartbeat_active_turns
    with _heartbeat_lock:
        _heartbeat_active_turns = max(0, _heartbeat_active_turns - 1)


# ponytail: read-only + light task mgmt. No write/edit/run.
_HEARTBEAT_ALLOWED_TOOLS = frozenset({
    "read_file", "grep_files", "git_status", "git_diff",
    "list_dir", "get_time", "list_tasks", "complete_task",
    "run_cmd", "pip_install",
})


@dataclass
class ContextDelta:
    """Result of the pre-check tick. Empty = nothing changed."""
    overdue_tasks: list[str] = field(default_factory=list)
    delegate_pending: bool = False
    git_changes: str = ""
    new_reports: list[str] = field(default_factory=list)
    cron_failures: list[str] = field(default_factory=list)
    session_active: bool = False  # Ralph loop: unfinished session exists
    test_failure: str = ""  # test output if tests are failing

    @property
    def is_empty(self) -> bool:
        return not (self.overdue_tasks or self.delegate_pending
                    or self.git_changes or self.new_reports or self.cron_failures
                    or self.session_active or self.test_failure)

    def summary(self) -> str:
        parts = []
        if self.overdue_tasks:
            parts.append(f"overdue tasks: {', '.join(self.overdue_tasks[:3])}")
        if self.delegate_pending:
            parts.append("pending delegate tasks")
        if self.git_changes:
            parts.append(f"git dirty ({self.git_changes[:100]})")
        if self.new_reports:
            parts.append(f"new reports: {', '.join(self.new_reports)}")
        if self.cron_failures:
            parts.append(f"cron failures: {', '.join(self.cron_failures)}")
        if self.session_active:
            parts.append("active session (unfinished task)")
        if self.test_failure:
            parts.append(f"tests failing: {self.test_failure[:100]}")
        return "; ".join(parts)


class HeartbeatEngine:
    """Background loop: pre-check → decide → act/notify."""

    def __init__(
        self,
        db: CrowState | None = None,
        cron_engine: Any | None = None,
        send_fn: Callable[[str], None] | None = None,
        tool_registry: Any | None = None,
        provider: Any | None = None,
        provider_manager: Any | None = None,
        project_root: str | Path | None = None,
        max_actions_per_hour: int = 3,  # ponytail: lowered from 6
        chat_id: int = 0,
        crow_log_fn: Callable[[str], Any] | None = None,
        self_model: Any | None = None,
    ) -> None:
        self._db = db
        self._cron = cron_engine
        self._send_fn = send_fn
        self._crow_log_fn = crow_log_fn
        self._tools = tool_registry
        self._provider = provider
        self._provider_manager = provider_manager
        self._project_root = Path(project_root) if project_root else Path.cwd()
        self._max_actions_per_hour = max_actions_per_hour
        self._chat_id = chat_id

        # Self-model: push health + initiative stats (Phase 3)
        if self_model is not None:
            self._self_model = self_model
        elif db is not None:
            from .self_model import SelfModel
            self._self_model = SelfModel(db_path=str(db._path))
        else:
            self._self_model = None

        # Named-slice gating (default: all enabled)
        self._slice_is_enabled = lambda name: True
        self._enabled_slices: set[str] = set()

        # Foreman: monitors crew tasks via scratchpad (Phase 9)
        from .scratchpad import CrewScratchpadDB
        from .foreman import Foreman
        self._scratchpad = CrewScratchpadDB(
            db_path=str(db._path) if db else ":memory:"
        ) if db else CrewScratchpadDB(":memory:")
        self._foreman = Foreman(scratchpad=self._scratchpad)
        self._last_foreman_tick: float = 0
        self._last_goal_cleanup: float = 0

        self._autonomous_sid = "__autonomous__"
        self._session_ready = False

        self._task: asyncio.Task | None = None
        self._resolution = 600  # ponytail: 10 min (was 5)
        self._notified: set[str] = set()
        self._last_snapshot: dict[str, Any] = {}
        self._consecutive_empty = 0
        self._action_timestamps: list[float] = []
        self._tick_count = 0
        self._last_code_check: float = 0
        self._last_deep_scan: float = 0
        self._last_health_check: float = 0
        self._current_delta: Any = None
        self._cron_log_cursor: int = 0  # bytes offset for new errors
        # Initiative rate limiting (ADR-0005)
        self._initiative_timestamps: list[float] = []
        self._initiative_consecutive: int = 0
        self._initiative_paused: bool = False
        # Daily agenda (proactive autonomy)
        self._daily_agenda_last_run: float = 0
        self._daily_agenda_items: list[str] = []
        self._active_initiatives: dict[str, dict] = {}
        # ponytail: track session_state.md re-spawns to prevent infinite loops
        # key = hash of session_state.md content[:200], value = (spawn_count, last_spawn_time)
        self._session_state_spawns: dict[int, tuple[int, float]] = {}
        # Register callback so mark_user_active can reset pause (ADR-0005)
        global _reset_initiative_pause
        _reset_initiative_pause = self._reset_initiative
        # Persist cursor across restarts to avoid re-queuing old errors
        cursor_path = Path.home() / ".crow_agent" / "cron_cursor.txt"
        if cursor_path.exists():
            try:
                self._cron_log_cursor = int(cursor_path.read_text().strip())
            except Exception:
                self._cron_log_cursor = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("Heartbeat started (interval=%ds)", self._resolution)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("Heartbeat stopped")

    # ── main loop ──────────────────────────────────────────────────

    async def _loop(self) -> None:
        """Main heartbeat loop. Iterates registered slice hooks."""
        # Hook registry: (name, interval_secs, slice_id, handler)
        hooks = [
            ("continue_initiatives", 0, "0", self._slice_continue_initiatives),
            ("notify",           0, "1", self._slice_notify),
            ("decide_act",       0, "4", self._slice_decide_and_act),
            ("code_check",    1800, "6", self._slice_code_check),
            ("reflect",       3600, "7", self._slice_reflect),
            ("user_model_compact", 86400, "8", self._slice_compact_user_model),
        ]
        last_run: dict[str, float] = {}
        # Named-slice gating (Phase 3). Supports:
        #   HEARTBEAT_ENABLE_SLICES="all" or "none"
        #   HEARTBEAT_ENABLE_SLICES="continue,notify,code" (named)
        #   HEARTBEAT_ENABLE_SLICES="0,1,6,7" (numeric, backward compat)
        #   CROW_HEARTBEAT_SLICES (fallback for backward compat)
        enabled_raw = os.environ.get(
            "HEARTBEAT_ENABLE_SLICES",
            os.environ.get("CROW_HEARTBEAT_SLICES", ""),
        )
        enabled = set(
            s.strip().lower() for s in enabled_raw.split(",") if s.strip()
        )

        def _slice_is_enabled(name: str) -> bool:
            if not enabled or "all" in enabled:
                return True
            if "none" in enabled:
                return False
            if name.lower() in enabled:
                return True
            for hname, _, sid, _ in hooks:
                if hname == name and sid in enabled:
                    return True
            return False

        self._enabled_slices = enabled
        self._slice_is_enabled = _slice_is_enabled

        while True:
            await asyncio.sleep(self._resolution)

            # Initiative: heartbeat ticks freely even during user chat

            # Event-driven trigger file check
            trigger = Path.home() / ".crow_agent" / "trigger_check"
            if trigger.exists():
                try:
                    trigger.unlink()
                except OSError:
                    pass

            delta = self._pre_check()
            self._tick_count += 1

            if delta.is_empty:
                has_session = (Path.home() / ".crow_agent" / "session_state.md").exists()
                if not has_session:
                    self._consecutive_empty += 1
                    if self._consecutive_empty >= 3:
                        await asyncio.sleep(self._resolution)
                    continue
                else:
                    self._consecutive_empty = 0

            self._consecutive_empty = 0
            self._current_delta = delta

            # Dispatch registered hooks
            for name, interval, slice_id, handler in hooks:
                if not _slice_is_enabled(name):
                    continue
                if time.time() - last_run.get(name, 0) < interval:
                    continue
                try:
                    last_run[name] = time.time()
                    await handler()
                except Exception as e:
                    logger.debug("Heartbeat hook '%s' failed: %s", name, e)

            # Foreman tick: monitor crew tasks every 60s (Phase 9)
            if time.time() - self._last_foreman_tick >= 60:
                self._last_foreman_tick = time.time()
                try:
                    self._foreman.tick()
                except Exception:
                    pass

            # Goal cleanup: abandon stale goals hourly
            if time.time() - self._last_goal_cleanup >= 3600:
                self._last_goal_cleanup = time.time()
                try:
                    n = self._db.abandon_stale_goals(days=7)
                    if n > 0:
                        logger.info("Abandoned %d stale goal(s)", n)
                except Exception:
                    pass

    # ── slice handlers ────────────────────────────────────────────

    async def _slice_continue_initiatives(self) -> None:
        """Rescue waiting/stuck tasks from active_tasks/ folder (Option C).

        Scans .crow_agent/active_tasks/*.json for tasks with status=waiting
        or retry_count under threshold. Spawns a continuation turn.
        """
        if not self._provider or not self._can_act():
            return
        import json, time, pathlib
        now = time.time()
        tasks_dir = pathlib.Path.home() / ".crow_agent" / "active_tasks"
        if not tasks_dir.exists():
            return

        rescued = 0
        for cp_path in sorted(tasks_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            if rescued >= 1:  # one per tick
                break
            try:
                cp = json.loads(cp_path.read_text())
                if cp.get("status") in ("waiting",):
                    goal = cp.get("goal", "Resume task")
                    logger.info("Heartbeat rescuing initiative %s", cp.get("session_id", "?"))
                    await self._spawn_initiative(goal, initiative_id=cp.get("session_id", "").replace("initiative_", ""))
                    rescued += 1
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Skipping corrupted task file %s: %s", cp_path.name, e)

    async def _slice_code_check(self) -> None:
        """Slice 6: autonomous code check on git changes."""
        if self._current_delta.git_changes:
            await self._auto_code_check(self._current_delta)

    async def _slice_reflect(self) -> None:
        """Slice 7: ponytail — reflect on recent work, journal one insight."""
        if not self._db or not self._provider:
            return
        try:
            # Get last 10 turns from any session
            conn = self._db._conn
            rows = conn.execute(
                "SELECT content FROM turns ORDER BY id DESC LIMIT 10"
            ).fetchall()
            if len(rows) < 5:
                return  # not enough data
            summary = "\n".join(r[0][:300] for r in reversed(rows))
            from crow_agent.providers import ChatMessage
            msgs = [
                ChatMessage(role="system", content="Review these recent turns. Output ONE short insight about patterns, mistakes, or improvements. One paragraph. No lists."),
                ChatMessage(role="user", content=summary),
            ]
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, self._provider.chat, msgs, None, 200)
            insight = resp.content.strip()
            if len(insight) > 20:
                vault = Path.home() / ".crow_agent" / "memory vault" / "reflect.md"
                vault.parent.mkdir(parents=True, exist_ok=True)
                ts = datetime.now(timezone.utc).isoformat()
                with open(vault, "a") as f:
                    f.write(f"\n## {ts}\n{insight}\n")
                logger.info("Reflect: %s", insight[:100])
        except Exception as e:
            logger.warning("Reflect failed: %s", e)

    async def _slice_decide_and_act(self) -> None:
        """Slices 4+5: proactive decision + action."""
        if self._provider and self._can_act():
            await self._decide_and_act(self._current_delta)

    async def _slice_notify(self) -> None:
        """Slice 1: notify on non-empty delta."""
        if not self._current_delta.is_empty and not self._provider:
            await self._notify(self._current_delta)
        # ponytail: hourly prune of stale done tasks
        try:
            from crow_agent.task_registry import _prune_stale
            _prune_stale()
        except Exception:
            pass

    # ── pre-check ──────────────────────────────────────────────────

    def _pre_check(self) -> ContextDelta:
        """Cheap file/env scans. No LLM call."""
        delta = ContextDelta()

        # Ralph loop: check for active session
        delta.session_active = (Path.home() / ".crow_agent" / "session_state.md").exists()

        try:
            from .task_registry import has_pending
            delta.delegate_pending = has_pending()
        except Exception:
            pass

        if self._db:
            try:
                tasks = self._db.list_tasks()
                now = datetime.now(timezone.utc).isoformat()
                overdue = []
                for t in tasks:
                    dl = t.get("deadline")
                    if dl and dl < now and t.get("status") not in ("done", "cancelled"):
                        overdue.append(t.get("title", "untitled"))
                delta.overdue_tasks = overdue
            except Exception:
                pass

        try:
            r = subprocess.run(
                ["git", "diff", "--stat"],
                capture_output=True, text=True, timeout=10,
                cwd=str(self._project_root),
            )
            if r.returncode == 0 and r.stdout.strip():
                delta.git_changes = r.stdout.strip()[:500]
        except Exception:
            pass

        reports_dir = Path.home() / ".crow_agent" / "reports"
        if reports_dir.exists():
            today = datetime.now().strftime("%Y-%m-%d")
            try:
                delta.new_reports = [
                    f.name for f in reports_dir.iterdir()
                    if f.suffix == ".md" and today in f.name
                ]
            except Exception:
                pass

        if self._cron:
            try:
                failed = []
                for job in self._cron.jobs():
                    if job.consecutive_failures >= 3 and not job.enabled:
                        failed.append(job.id)
                    elif job.consecutive_failures > 0:
                        failed.append(job.id)
                delta.cron_failures = failed
            except Exception:
                pass

        snap = {"delegate": delta.delegate_pending, "overdue": len(delta.overdue_tasks), "cron": len(delta.cron_failures)}
        if snap == self._last_snapshot:
            return ContextDelta()
        self._last_snapshot = snap
        return delta

    # ── cross-turn memory (Slice 3) ────────────────────────────────

    def _ensure_session(self) -> None:
        if not self._session_ready and self._db:
            try:
                self._db.create_session(self._autonomous_sid)
            except Exception:
                pass
            self._session_ready = True

    # Actions worth logging to Crow Log channel (skip noise)
    _CROW_LOG_ACTIONS = frozenset({
        "DECIDE", "REFLECT", "ACT", "INFORM", "INVESTIGATE",
        "CODECHK", "SCAN", "IMPLEMENT", "AGENDA", "AGENDA_ACT",
        "PROCESS", "HEALTH", "FAIL", "NOTIFY",
    })

    def _store_tick(self, action: str, detail: str) -> None:
        if not self._db:
            return
        self._ensure_session()
        entry = f"[heartbeat] {action} — {detail[:500]}"
        try:
            self._db.append_turn(self._autonomous_sid, "assistant", entry)
        except Exception as exc:
            logger.debug("Failed to store tick: %s", exc)
        # Crow Log: channel activity feed
        if self._crow_log_fn and action in self._CROW_LOG_ACTIONS:
            try:
                result = self._crow_log_fn(f"⏰ {action}: {detail[:300]}")
                # Handle both sync and async callbacks
                if hasattr(result, '__await__'):
                    import asyncio
                    asyncio.get_event_loop().create_task(result)
            except Exception:
                pass  # ponytail: best-effort, never block heartbeat

    def _recent_ticks(self, n: int = 3) -> str:
        if not self._db:
            return ""
        self._ensure_session()
        try:
            turns = self._db.history(self._autonomous_sid, limit=n)
            lines = []
            for t in turns:
                if t.get("role") == "assistant":
                    content = t.get("content", "")
                    if content.startswith("[heartbeat]"):
                        lines.append(content[len("[heartbeat]"):].strip())
            if lines:
                return "Recent heartbeat activity:\n" + "\n".join(f"  • {l}" for l in lines)
        except Exception as exc:
            logger.debug("Failed to read tick history: %s", exc)
        return ""

    # ── decide + act ──────────────────────────────────────────────

    async def _decide_daily_agenda(self) -> None:
        """Proactive daily agenda: survey data, rank what Crow should work on.

        Runs once per day. Uses cheap LLM (opencode-zen) to review:
        1. Yesterday's autonomous session ticks
        2. Open tasks from task registry
        3. Recent user messages across all sessions

        Returns a ranked list, stores in autonomous session.
        If top item exists and can act, spawns Initiative.
        """
        import time as _time
        now = _time.time()
        if now - self._daily_agenda_last_run < 86400:
            return
        self._daily_agenda_last_run = now

        if not self._provider or not self._can_act():
            return

        # Gather data
        recent_ticks = self._recent_ticks(n=5)

        open_tasks_str = "(none)"
        try:
            from crow_agent.task_registry import _tasks
            # _tasks is a module-level dict; access is thread-safe for reads
            open_tasks = [
                t for t in list(_tasks.values())
                if t.state in ("pending",)
            ]
            if open_tasks:
                lines = []
                for t in open_tasks[:10]:
                    lines.append(f"- [{t.id[:8]}] {t.prompt[:120]}")
                open_tasks_str = "\n".join(lines)
        except Exception:
            pass

        recent_user_msgs = "(none)"
        try:
            if self._db:
                # Query sessions directly (no list_sessions on CrowState yet)
                rows = self._db._conn.execute(
                    "SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 5"
                ).fetchall()
                user_lines: list[str] = []
                one_hour_ago = time.time() - 3600
                for (sid,) in rows:
                    if sid == self._autonomous_sid:
                        continue
                    turns = self._db.history(sid, limit=2)
                    for t in turns:
                        if t.get("role") == "user":
                            # Skip recent messages — main turn handles them
                            created = t.get("created_at", "")
                            if created:
                                try:
                                    from datetime import datetime
                                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                                    if dt.timestamp() > one_hour_ago:
                                        continue
                                except ValueError:
                                    pass
                            content = t.get("content", "")[:200]
                            if content.strip():
                                user_lines.append(f"[{sid[:8]}] {content}")
                if user_lines:
                    recent_user_msgs = "\n".join(user_lines[-5:])
        except Exception:
            pass

        prompt = (
            "Morning planner. Review what happened yesterday, "
            "what's pending, and what the user has been asking about. "
            "Return a ranked list of 1-3 things Crow should work on today. "
            "Format each as: N. [one-line goal]\n"
            "Be specific and actionable. If nothing needs attention, say 'NOTHING'.\n\n"
            f"Yesterday's activity:\n{recent_ticks}\n\n"
            f"Open tasks:\n{open_tasks_str}\n\n"
            f"Recent user messages:\n{recent_user_msgs}"
        )

        from .providers import ChatMessage
        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                self._provider.chat,
                [ChatMessage(role="user", content=prompt)],
                None,
            )
        except Exception as exc:
            logger.warning("Daily agenda LLM call failed: %s", exc)
            return

        agenda_text = (resp.content or "").strip()
        if not agenda_text or "NOTHING" in agenda_text.upper():
            self._store_tick("AGENDA", "nothing to work on")
            return

        # Parse ranked items
        items: list[str] = []
        for line in agenda_text.split("\n"):
            line = line.strip()
            if line and (line[0].isdigit() and "." in line[:3]):
                items.append(line)
        self._daily_agenda_items = items

        self._store_tick("AGENDA", agenda_text[:500])
        logger.info("Daily agenda: %d items", len(items))

        # Spawn Initiative for top item
        if items and self._can_act():
            top = items[0]
            # Strip the "1. " prefix
            goal = top.split(". ", 1)[-1] if ". " in top else top
            self._store_tick("AGENDA_ACT", goal[:200])
            await self._spawn_initiative(goal)

    def _can_act(self) -> bool:
        now = time.time()
        self._action_timestamps = [t for t in self._action_timestamps if now - t < 3600]
        return len(self._action_timestamps) < self._max_actions_per_hour

    def _reset_initiative(self) -> None:
        """Reset initiative pause — called by mark_user_active when user interacts."""
        self._initiative_consecutive = 0
        self._initiative_paused = False

    async def _decide_and_act(self, delta: ContextDelta) -> None:
        """Call cheap LLM with expanded action space."""
        recent = self._recent_ticks()
        action_desc = (
            f"INFORM <reason> — notify the user\n"
            f"INVESTIGATE <tool_name> <JSON arguments> — run a read-only tool\n"
            f"ACT <goal> — spawn Initiative agent turn to solve a detected problem\n"
            f"PROCESS delegates — drain pending delegate tasks\n"
            f"NOTHING — skip"
        )

        prompt = (
            f"Background monitor. Context: {delta.summary()}\n"
            + (f"{recent}\n" if recent else "")
            + f"\nDecide. Reply EXACTLY ONE of:\n{action_desc}\n\n"
            f"Allowed tools: {', '.join(sorted(_HEARTBEAT_ALLOWED_TOOLS))}"
        )

        from .providers import ChatMessage
        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(None, self._provider.chat, [ChatMessage(role="user", content=prompt)], None)
        except Exception as exc:
            logger.warning("Heartbeat LLM decision failed: %s", exc)
            if not delta.is_empty:
                await self._notify(delta)
            return

        decision = (resp.content or "").strip()
        self._action_timestamps.append(time.time())
        self._store_tick("DECIDE", decision[:200])

        if decision.startswith("PROCESS"):
            await self._process_delegates(delta)
        elif decision.startswith("REFLECT"):
            observation = decision[len("REFLECT"):].strip()
            self._store_tick("REFLECT", observation)
            logger.info("Heartbeat reflection: %s", observation)
        elif decision.startswith("INVESTIGATE"):
            parts = decision.split(maxsplit=2)
            if len(parts) >= 2:
                tool_name = parts[1]
                args = {}
                if len(parts) >= 3:
                    try:
                        args = json.loads(parts[2])
                    except json.JSONDecodeError:
                        args = {"path": parts[2].strip("\"'")}
                await self._investigate(delta, tool_name, args)
            else:
                await self._notify(delta)
        elif decision.startswith("ACT"):
            goal = decision[len("ACT"):].strip()
            self._store_tick("ACT", goal)
            await self._spawn_initiative(goal)
        elif decision.startswith("INFORM"):
            reason = decision[len("INFORM"):].strip()
            self._store_tick("INFORM", reason)
            await self._notify(delta, reason)
        else:
            logger.info("Heartbeat decided nothing — %s", decision[:100])
            # Always log delta changes for audit transparency
            if delta.git_changes:
                logger.info("Heartbeat: git dirty — %s", delta.git_changes[:200])
            if delta.new_reports:
                logger.info("Heartbeat: new reports — %s", delta.new_reports)
            if delta.overdue_tasks:
                logger.info("Heartbeat: %d overdue tasks", len(delta.overdue_tasks))
            if delta.cron_failures:
                logger.info("Heartbeat: %d cron failures", len(delta.cron_failures))

    async def _process_delegates(self, delta: ContextDelta) -> None:
        """Drain pending delegate tasks via existing task_registry machinery."""
        try:
            from .task_registry import drain_and_execute, get as get_task

            async def _hb_deliver(task_id: str, result: str, error: str | None) -> None:
                t = get_task(task_id)
                title = (t.prompt[:100] if t else "unknown") if t else "unknown"
                self._store_tick("PROCESS",
                    f"done {task_id[:8]}: {'ok' if not error else 'fail'}")
                if self._send_fn:
                    if error:
                        await self._send_fn(f"❌ Task _{task_id}_ failed: {error[:500]}")
                    else:
                        await self._send_fn(f"✅ Task _{task_id[:8]}_ done:\n{title}\n{result[:1000]}")

            await drain_and_execute(deliver=_hb_deliver)
            self._store_tick("PROCESS", "delegate queue drained")
            logger.info("Heartbeat processed pending delegates")
        except Exception as exc:
            logger.warning("Heartbeat process failed: %s", exc)
            if not delta.is_empty:
                await self._notify(delta)


    async def _spawn_initiative(self, goal: str, initiative_id: str | None = None, profile_name: str | None = None) -> None:
        """Spawn an Initiative agent turn to act on a heartbeat detection.

        Creates a new AIAgent, runs one turn, classifies output, updates
        checkpoint status. If profile_name is given or detected from goal,
        uses the team profile for specialized roles."""
        import uuid, time as _time
        from .run_agent import AIAgent, Trigger, TriggerSource, _save_checkpoint, _load_checkpoint
        from .agent_profiles import load_profile

        # Auto-detect profile from goal if not specified
        if not profile_name:
            _lower = goal.lower()
            if "test" in _lower and ("fail" in _lower or "fix" in _lower):
                profile_name = "code-worker"
            elif "code" in _lower and ("review" in _lower or "check" in _lower):
                profile_name = "verifier"
            elif "bug" in _lower or "error" in _lower or "crash" in _lower or "fail" in _lower:
                profile_name = "code-worker"
            elif "architect" in _lower or "design" in _lower or "decision" in _lower:
                profile_name = "architect"
            elif "research" in _lower or "investigate" in _lower or "find" in _lower:
                profile_name = "web-reader"
            elif "implement" in _lower or "build" in _lower or "create" in _lower:
                profile_name = "deep-worker"
            elif "verify" in _lower or "check" in _lower or "validate" in _lower:
                profile_name = "verifier"

        now = _time.time()
        is_continuation = initiative_id is not None and initiative_id in self._active_initiatives
        iid = initiative_id if is_continuation else uuid.uuid4().hex[:8]

        # Rate limit: only count NEW initiatives, not continuations
        if not is_continuation:
            self._initiative_timestamps = [t for t in self._initiative_timestamps if now - t < 3600]
            if len(self._initiative_timestamps) >= 2:
                logger.info("Initiative rate limit hit (2/hr), deferring: %s", goal[:80])
                return
            if self._initiative_paused:
                logger.info("Initiative paused (3 consecutive without user), deferring: %s", goal[:80])
                return
            with _heartbeat_lock:
                user_recently_active = _heartbeat_last_user_interaction > 0 and (now - _heartbeat_last_user_interaction) < 120
            if user_recently_active and _heartbeat_active_turns == 0:
                logger.info("Initiative deferring — user was recently active: %s", goal[:80])
                return
            self._initiative_timestamps.append(now)
            self._initiative_consecutive += 1
            if self._initiative_consecutive >= 3:
                self._initiative_paused = True
                logger.warning("Initiative paused after 3 consecutive turns without user interaction")

        sid = f"initiative_{iid}"

        if is_continuation:
            state = self._active_initiatives[iid]
            state["status"] = "active"
            turn_count = state.get("turn_count", 0) + 1
            state["turn_count"] = turn_count
            last_output = state.get("last_output", "")
            last_tools = state.get("last_tools", [])
        else:
            state = {"goal": goal, "status": "active", "last_output": "", "last_tools": [], "turn_count": 1, "outcome": "pending", "_started_at": time.time()}
            self._active_initiatives[iid] = state
            turn_count = 1
            last_output = ""
            last_tools = []

        # Load checkpoint for continuation
        cp = _load_checkpoint(sid) if is_continuation else None
        round_num = (cp.get("round", 0) + 1) if cp else 1
        discoveries = cp.get("discoveries", []) if cp else []
        tools_used = cp.get("tools_used", []) if cp else []
        retry_count = cp.get("retry_count", 0) if cp else 0

        try:
            # Resolve worker-specific provider when a profile is matched
            if profile_name and self._provider_manager:
                try:
                    from .crew import get_worker_provider
                    _agent_provider = get_worker_provider(profile_name, self._provider_manager)
                    logger.info("Initiative resolved worker provider for '%s'", profile_name)
                except Exception as exc:
                    logger.warning("Failed to resolve worker provider for '%s', falling back: %s", profile_name, exc)
                    _agent_provider = self._provider
            else:
                _agent_provider = self._provider

            # Build context lines for identity
            context_lines = [f"Current task: {goal}"]
            if is_continuation and turn_count > 1:
                context_lines.append(f"Turn {turn_count} — continuing")

            # Use profile agent if one was matched, otherwise fall back to raw AIAgent
            profile = load_profile(profile_name) if profile_name else None
            if profile:
                logger.info("Initiative using profile: %s (for: %s)", profile_name, goal[:60])
                identity_parts = [profile.instructions]
                if context_lines:
                    identity_parts.append("\n".join(context_lines))
                identity = "\n\n".join(identity_parts)

                trigger_prompt = goal
                if is_continuation and discoveries:
                    trigger_prompt = (
                        f"[TASK UPDATE — round {round_num}]\n"
                        f"Goal: {goal}\n\n"
                        f"Progress:\n"
                        + "\n".join(f"  \u2705 {d[:200]}" for d in discoveries[-3:]) + "\n\n"
                        f"Continue with the next step."
                    )

                agent = AIAgent(
                    session_id=sid,
                    provider=_agent_provider,
                    tool_registry=self._tools,
                    identity=identity,
                )
                trigger = Trigger(
                    source=TriggerSource.HEARTBEAT,
                    prompt=trigger_prompt,
                    initiative_id=iid,
                )

                output_parts = []
                tool_calls: list[str] = []
                async for event in agent.run_stream(trigger):
                    if isinstance(event, dict):
                        if event.get("type") == "final":
                            output_parts.append(event.get("text", ""))
                        elif event.get("type") == "tool" and event.get("status") == "start":
                            tool_calls.append(event.get("name", "?"))
                        elif event.get("type") == "tool" and event.get("status") == "error":
                            tool_calls.append(f"{event.get('name', '?')} \u2716")
                    elif isinstance(event, str):
                        output_parts.append(event)

                final_output = "".join(output_parts)
            else:
                agent = AIAgent(
                    session_id=sid,
                    provider=_agent_provider,
                    tool_registry=self._tools,
                    identity=(
                        "Autonomous action agent. "
                        f"Current task: {goal}\n\n"
                        "Act on this task using your full tool set. "
                        "When done, append [DONE] to your final response. "
                        "If work continues next turn, append [CONTINUE]."
                    ),
                )
                trigger_prompt = goal
                if is_continuation and discoveries:
                    trigger_prompt = (
                        f"[TASK UPDATE — round {round_num}]\n"
                        f"Goal: {goal}\n\n"
                        f"Progress:\n"
                        + "\n".join(f"  \u2705 {d[:200]}" for d in discoveries[-3:]) + "\n\n"
                        f"Continue with the next step."
                    )
                trigger = Trigger(
                    source=TriggerSource.HEARTBEAT,
                    prompt=trigger_prompt,
                    initiative_id=iid,
                )
                output_parts = []
                tool_calls: list[str] = []
                async for event in agent.run_stream(trigger):
                    if isinstance(event, dict):
                        if event.get("type") == "final":
                            output_parts.append(event.get("text", ""))
                        elif event.get("type") == "tool" and event.get("status") == "start":
                            tool_calls.append(event.get("name", "?"))
                        elif event.get("type") == "tool" and event.get("status") == "error":
                            tool_calls.append(f"{event.get('name', '?')} ❌")
                    elif isinstance(event, str):
                        output_parts.append(event)
                final_output = "".join(output_parts)

            # Detect fake completions: text-only "acknowledged" without real action
            had_tools = len(tool_calls) > 0
            had_content = bool(final_output.strip()) and "Read-lock engaged" not in final_output
            is_error = "Both providers failed" in final_output or "\u26a0" in final_output

            # Save checkpoint with status
            cp_status = "waiting" if (had_tools or is_error) else "done"
            _save_checkpoint(sid, goal, round_num, tool_calls, final_output)
            # Update status + retry_count in checkpoint
            import json, pathlib
            cp_path = pathlib.Path.home() / ".crow_agent" / "active_tasks" / f"{sid}.json"
            if cp_path.exists():
                cp_data = json.loads(cp_path.read_text())
                cp_data["status"] = cp_status
                cp_data["retry_count"] = retry_count + (1 if is_error else 0)
                cp_path.write_text(json.dumps(cp_data, indent=2, ensure_ascii=False))

            if cp_status == "done":
                logger.info("Initiative #%s completed (round %d)", iid, round_num)
                pathlib.Path.unlink(cp_path, missing_ok=True)
                await self._advance_agenda()
            elif is_error:
                logger.warning("Initiative #%s error (retry %d) — will retry", iid, retry_count + 1)
            else:
                logger.info("Initiative #%s turn %d continuing", iid, round_num)

            # Build tool summary
            _tool_icons_map = {
                "run_cmd": "💻", "read_file": "📖", "write_file": "✍️",
                "edit_file": "✏️", "grep_files": "🔍", "git_status": "📋",
                "git_diff": "📊", "git_log": "📜", "web_search": "🌐",
                "web_fetch": "📥", "list_dir": "📂", "get_time": "🕐",
                "delegate_task": "📤", "spawn_agent": "🤖",
                "list_tasks": "📝", "complete_task": "✅",
            }
            from collections import Counter
            tool_summary = Counter(tool_calls)
            tool_lines = []
            for name, count in tool_summary.most_common(10):
                _icon = _tool_icons_map.get(name, "🛠️")
                tool_lines.append(f"{_icon} {name}" + (f" (x{count})" if count > 1 else ""))
            _tools_text = "\n".join(tool_lines) if tool_lines else "(no tools used)"

            # Store result in autonomous session
            self._db.append_turn(
                self._autonomous_sid, "assistant",
                f"[Initiative #{iid}] {goal}\n\n{final_output[:2000]}"
            )

            # Send to Crow Log Telegram channel
            try:
                from .telegram_bot import _bot_instance, send_to_crow_log
                if _bot_instance and _bot_instance._app and _bot_instance._app.bot:
                    summary = final_output[:400] if final_output else ""
                    msg = f"{goal}\n\n{_tools_text}"
                    if summary:
                        msg += f"\n\n{summary}"
                    await send_to_crow_log(_bot_instance._app.bot, msg, iid)
            except Exception as e:
                logger.warning("Crow Log notify failed: %s", e)

            logger.info("Initiative #%s turn %d finished: %s", iid, turn_count, goal[:80])

        except Exception as e:
            logger.error("Initiative #%s crashed: %s", iid, exc, exc_info=True)
            _save_checkpoint(sid, goal, 0, [], f"Crashed: {e}")
            import json, pathlib as _pl
            _fp = _pl.Path.home() / ".crow_agent" / "active_tasks" / f"{sid}.json"
            if _fp.exists():
                _cd = json.loads(_fp.read_text())
                _cd["status"] = "waiting"
                _cd["retry_count"] = retry_count + 1
                _fp.write_text(json.dumps(_cd, indent=2))


    async def _advance_agenda(self) -> None:
        if not self._daily_agenda_items:
            return
        import hashlib
        for item in self._daily_agenda_items:
            iid = hashlib.md5(item.encode()).hexdigest()[:8]
            if iid not in self._active_initiatives or self._active_initiatives[iid].get('outcome') == 'completed':
                logger.info("Agenda advance: spawning '%s'", item[:80])
                await self._spawn_initiative(item)
                return
        logger.info("Agenda: all items started or completed")

    # ── autonomous coding (Slice 6) ───────────────────────────────

    async def _auto_code_check(self, delta: ContextDelta) -> None:
        """When git changes detected: run tests, fix failures, report."""
        # Step 1: get the full diff to understand scope
        try:
            r = subprocess.run(
                ["git", "diff"],
                capture_output=True, text=True, timeout=10,
                cwd=str(self._project_root),
            )
            diff = r.stdout[:3000] if r.returncode == 0 else ""
        except Exception:
            diff = ""

        # Step 2: run tests
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/", "-x", "--tb=short"],
                capture_output=True, text=True, timeout=120,
                cwd=str(self._project_root),
            )
        except subprocess.TimeoutExpired:
            self._store_tick("CODECHK", "tests timed out (>120s)")
            await self._send_fn("⚠️ Tests timed out after your code changes. Check manually.")
            return

        test_out = r.stdout + "\n" + r.stderr
        test_passed = r.returncode == 0

        if test_passed:
            self._store_tick("CODECHK", f"all tests pass after change ({diff[:100]})")
            logger.info("Heartbeat code check: all tests pass")
            return

        # Step 3: tests failed — diagnose and fix
        # ponytail: suppress repeat alerts within 60 min — prevents crew loop
        now = time.time()
        if hasattr(self, '_last_test_fail_time') and (now - self._last_test_fail_time) < 3600:
            logger.info("Heartbeat code check: tests still failing, suppressed (cooldown)")
            return
        self._last_test_fail_time = now
        self._store_tick("CODECHK", f"tests failing")
        logger.warning("Heartbeat code check: tests FAILING")
        import subprocess as _sp
        author=_sp.run(["git","log","--oneline","-1","--format=%an"],capture_output=True,text=True,timeout=5,cwd=str(self._project_root)).stdout.strip().lower()
        if "crow" in author:
            logger.warning("Crow commit caused failure, reverting")
            _sp.run(["git","revert","--no-edit","HEAD"],capture_output=True,timeout=10,cwd=str(self._project_root))
            _sp.run(["sudo","systemctl","restart","crow-agent"],capture_output=True,timeout=10)
            self._store_tick("ROLLBACK","auto-reverted Crow commit")

        # Pre-check: ModuleNotFoundError → pip install, no LLM needed
        import re as _re
        _mod_match = _re.search(r"ModuleNotFoundError: No module named '(\w+)'", test_out)
        if _mod_match:
            _module = _mod_match.group(1)
            logger.info("Heartbeat: installing missing module '%s'", _module)
            try:
                _r = subprocess.run(
                    [sys.executable, "-m", "pip", "install", _module],
                    capture_output=True, text=True, timeout=60,
                    cwd=str(self._project_root),
                )
                self._store_tick("CODECHK", f"pip install {_module}: {_r.stdout[-100:]}")
                # Re-run tests after install
                _r2 = subprocess.run(
                    [sys.executable, "-m", "pytest", "tests/", "-x", "--tb=short"],
                    capture_output=True, text=True, timeout=120,
                    cwd=str(self._project_root),
                )
                if _r2.returncode == 0:
                    self._store_tick("CODECHK", f"auto-fixed: installed {_module}, all tests pass")
                    await self._send_fn(f"✅ Auto-fixed: installed missing '{_module}' package. Tests pass.")
                    return
                else:
                    test_out = _r2.stdout + "\n" + _r2.stderr
            except Exception as _exc:
                logger.warning("pip install %s failed: %s", _module, _exc)

        # Route test failure through Initiative for full agent turn
        goal = f"Tests are failing. Fix them.\n\nTest output:\n{test_out[:1500]}"
        if diff:
            goal += f"\n\nRecent diff:\n{diff[:1000]}"
        self._store_tick("CODECHK", f"routing test failure to Initiative")
        await self._spawn_initiative(goal)

    # ── deep codebase scan (Slice 7: full autonomy) ───────────────

    async def _deep_codebase_scan(self) -> None:
        """Every 60 min: scan for TODOs, FIXMEs, stale branches, test gaps."""
        # Gather signals
        todos = []
        try:
            r = subprocess.run(
                ["grep", "-rn", "--include=*.py", "-i", r"(TODO|FIXME|HACK|XXX)"],
                capture_output=True, text=True, timeout=15,
                cwd=str(self._project_root),
            )
            if r.returncode in (0, 1):
                todos = r.stdout.strip()[:2000]
        except Exception:
            todos = ""

        git_log = ""
        try:
            r = subprocess.run(
                ["git", "log", "--oneline", "-15", "--no-merges"],
                capture_output=True, text=True, timeout=10,
                cwd=str(self._project_root),
            )
            git_log = r.stdout.strip()[:1000]
        except Exception:
            pass

        stale_branches = ""
        try:
            r = subprocess.run(
                ["git", "branch", "--merged=master", "--no-merged", "origin/master"],
                capture_output=True, text=True, timeout=10,
                cwd=str(self._project_root),
            )
            stale_branches = r.stdout.strip()[:500]
        except Exception:
            pass

        if not todos and not stale_branches:
            self._store_tick("SCAN", "no improvement opportunities found")
            return

        # LLM decides if anything worth acting on
        prompt = (
            f"Codebase scan results:\n\n"
            + (f"TODOs/FIXMEs:\n{todos}\n\n" if todos else "")
            + (f"Recent commits:\n{git_log}\n\n" if git_log else "")
            + (f"Stale branches:\n{stale_branches}\n\n" if stale_branches else "")
            + "Decide: IMPLEMENT <title>|<description of task> — queue a coding task\n"
            "or NOTHING — nothing worth doing now"
        )
        from .providers import ChatMessage
        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(
                None, self._provider.chat, [ChatMessage(role="user", content=prompt)], None
            )
        except Exception as exc:
            logger.debug("Heartbeat scan abort: %s", exc)
            return

        decision = (resp.content or "").strip()
        if decision.startswith("IMPLEMENT"):
            parts = decision[len("IMPLEMENT"):].strip().split("|", 1)
            if len(parts) >= 1:
                title = parts[0].strip()
                desc = parts[1].strip() if len(parts) > 1 else title
                # Queue as a delegate task for deep-worker
                task_prompt = (
                    f"[Auto-detected by heartbeat]\nTask: {title}\n\n{desc}\n\n"
                    f"Implement this in a new branch, add tests, verify all tests pass. "
                    f"Do NOT push or deploy."
                )
                try:
                    from .task_registry import enqueue
                    tid = enqueue(prompt=task_prompt, profile="deep-worker", chat_id=self._chat_id)
                    self._store_tick("IMPLEMENT", f"queued {tid}: {title[:100]}")
                    await self._send_fn(f"🛠 Found improvement opportunity — queued task _{tid}_:\n{title}")
                except Exception as exc:
                    self._store_tick("IMPLEMENT", f"failed to queue: {exc}")
        else:
            self._store_tick("SCAN", "no action taken")

    # ── self-heal health check (Slice 8) ──────────────────────────

    async def _check_goals(self) -> None:
        """Slice 8: Check active goals for progress. Queue next step if stalled."""
        try:
            from crow_agent.crow_state import CrowState
            db = CrowState()
            goals = db.list_goals()
            if not goals:
                db.close()
                return

            for goal in goals[:3]:  # max 3 goals per check
                progress = goal.get("progress", 0)
                title = goal.get("title", "Untitled")
                goal_id = goal.get("id", "")

                # Stalled: no progress in 24h or progress stuck
                if progress < 100:
                    from crow_agent.task_registry import enqueue, set_chat_id
                    set_chat_id(0)
                    enqueue(
                        prompt=(
                            f"Goal: {title} (progress: {progress}%). "
                            f"Plan and execute ONE step to advance this goal. "
                            f"After completing, update goal progress using update_progress. "
                            f"Goal ID: {goal_id}"
                        ),
                        profile="deep-worker",
                    )
                    logger.info("Heartbeat: queued step for goal '%s' (%d%%)", title, progress)

            db.close()
        except Exception as e:
            logger.debug("Goal check failed (non-blocking): %s", e)

    async def _pip_safety_check(self, pkg: str) -> bool:
        """Run pytest after pip install. Revert if tests fail."""
        import subprocess as _sp, sys as _sys
        try:
            r=_sp.run([_sys.executable,"-m","pytest","tests/","-x","-q","--tb=short"],capture_output=True,text=True,timeout=120,cwd=str(self._project_root))
            if r.returncode!=0:
                logger.warning("pip %s broke tests, reverting",pkg)
                _sp.run([_sys.executable,"-m","pip","uninstall","-y",pkg],capture_output=True,timeout=30)
                _sp.run(["git","stash"],capture_output=True,timeout=10,cwd=str(self._project_root))
                self._store_tick("PIPGUARD",f"reverted {pkg}")
                return False
            return True
        except Exception: return False

    async def _health_self_check(self) -> None:
        """Check Crow's own health signals, auto-repair when possible."""
        # Push health to SelfModel (Phase 3)
        self._push_health_to_self_model()

        issues = await self._scan_health_issues()
        if not issues:
            self._store_tick("HEALTH", "all clear")
            return

        for issue in issues:
            fixed = await self._attempt_repair(issue)
            if fixed:
                self._store_tick("HEALTH", f"fixed: {issue['desc'][:100]}")
            else:
                self._store_tick("HEALTH", f"unfixable: {issue['desc'][:100]}")
                # Queue as delegate task so Crow can diagnose + fix
                try:
                    from .task_registry import enqueue
                    task_prompt = (
                        f"[Auto-detected by health check]\nHealth issue: {issue['desc'][:500]}\n\n"
                        f"Diagnose and fix this issue on the VPS. Run commands to check the system, "
                        f"edit configs or scripts as needed. Test that the fix works. Do NOT deploy."
                    )
                    tid = enqueue(prompt=task_prompt, profile="deep-worker", chat_id=self._chat_id)
                    self._store_tick("HEALTH", f"queued {tid}")
                except Exception as exc:
                    self._store_tick("HEALTH", f"queue failed: {exc}")

    async def _scan_health_issues(self) -> list[dict]:
        """Check cron log, critical files, and recent errors."""
        issues = []

        # 1. New errors in cron log
        log_path = Path.home() / ".crow_agent" / "reports" / "cron.log"
        if log_path.exists():
            try:
                size = log_path.stat().st_size
                if self._cron_log_cursor > size:  # log was rotated
                    self._cron_log_cursor = 0
                if size > self._cron_log_cursor:
                    with open(log_path) as f:
                        f.seek(self._cron_log_cursor)
                        new_content = f.read()
                        self._cron_log_cursor = f.tell()
                    # Persist cursor across restarts
                    (Path.home() / ".crow_agent" / "cron_cursor.txt").write_text(str(self._cron_log_cursor))
                    # Look for error indicators
                    for line in new_content.splitlines():
                        if any(kw in line.lower() for kw in ("error", "traceback", "failed", "exception")):
                            issues.append({
                                "type": "cron_error",
                                "desc": f"New error in cron.log: {line[:200]}",
                            })
            except Exception:
                pass

        # 2. Critical scripts exist
        critical_scripts = [
            "/opt/crow-agent/scripts/sync-memory-vault.sh",
        ]
        for sp in critical_scripts:
            if not Path(sp).exists():
                issues.append({
                    "type": "missing_script",
                    "path": sp,
                    "desc": f"Missing critical script: {sp}",
                })

        # 3. Recent errors in autonomous session
        if self._db:
            try:
                turns = self._db.history(self._autonomous_sid, limit=5)
                for t in turns:
                    if "⚠️ Error" in (t.get("content", "") or ""):
                        issues.append({
                            "type": "agent_error",
                            "desc": f"Recent error in autonomous turn: {t.get('content', '')[:200]}",
                        })
                        break
            except Exception:
                pass

        return issues

    def _push_health_to_self_model(self) -> None:
        """Push disk/RAM/CPU health metrics to SelfModel (Phase 3)."""
        if not self._self_model:
            return
        try:
            import subprocess as _sp
            # Disk usage
            disk = _sp.run(["df", "/"], capture_output=True, text=True, timeout=5)
            disk_pct = 0
            for line in disk.stdout.strip().split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 5:
                    disk_pct = int(parts[4].replace("%", ""))
                    break
            # RAM
            mem = _sp.run(["free", "-m"], capture_output=True, text=True, timeout=5)
            lines = mem.stdout.strip().split("\n")
            if len(lines) > 1:
                mem_parts = lines[1].split()
                total_mem = int(mem_parts[1]) if len(mem_parts) > 1 else 1
                used_mem = int(mem_parts[2]) if len(mem_parts) > 2 else 0
                ram_pct = int(used_mem / total_mem * 100) if total_mem else 0
            else:
                ram_pct = 0
            # CPU load
            load = _sp.run(["cat", "/proc/loadavg"], capture_output=True, text=True, timeout=5)
            cpu_load = float(load.stdout.strip().split()[0]) if load.stdout.strip() else 0.0

            self._self_model.update("status.health", {
                "disk_pct": disk_pct,
                "ram_pct": ram_pct,
                "cpu_load": cpu_load,
                "last_check": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            self._self_model.update("heartbeat", {
                "running": True,
                "tick_interval_seconds": self._resolution,
                "last_tick": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
        except Exception:
            pass  # ponytail: best-effort

    async def _attempt_repair(self, issue: dict) -> bool:
        """Try to fix a health issue. Returns True if fixed."""
        if issue["type"] == "missing_script":
            sp = issue.get("path", "")
            if "sync-memory-vault" in sp:
                return await self._rebuild_sync_script(sp)
        return False  # can't auto-repair

    async def _rebuild_sync_script(self, path: str) -> bool:
        """Recreate the Tailscale vault sync script from known spec."""
        laptop = os.environ.get("CROWD_LAPTOP_SSH", "")
        if not laptop:
            logger.warning("Cannot rebuild sync script: CROWD_LAPTOP_SSH not set")
            return False
        vault_path = os.environ.get("CROWD_VAULT_PATH", "/opt/crow-agent/memory-vault")
        laptop_vault = os.environ.get("CROWD_LAPTOP_VAULT", "~/Desktop/Crow Agent/memory vault")
        content = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "LOG=\"/root/.crow_agent/reports/sync-vault.log\"\n"
            "echo \"[$(date)] Starting vault sync...\" >> \"$LOG\"\n"
            "# Bidirectional rsync with Tailscale\n"
            "rsync -avz --delete -e \"ssh -o StrictHostKeyChecking=no\" \\\\\n"
            "    " + vault_path + "/ " + laptop + ":" + laptop_vault + "/ \\\\\n"
            "    >> \"$LOG\" 2>&1 || echo \"[$(date)] WARN: push failed\" >> \"$LOG\"\n"
            "rsync -avz --delete -e \"ssh -o StrictHostKeyChecking=no\" \\\\\n"
            "    " + laptop + ":" + laptop_vault + "/ " + vault_path + "/ \\\\\n"
            "    >> \"$LOG\" 2>&1 || echo \"[$(date)] WARN: pull failed\" >> \"$LOG\"\n"
            "echo \"[$(date)] Sync complete.\" >> \"$LOG\"\n"
        )
        try:
            Path(path).write_text(content)
            Path(path).chmod(0o755)
            logger.info("Recreated missing script: %s", path)
            self._store_tick("HEALTH", f"recreated {path}")
            return True
        except Exception as exc:
            logger.warning("Failed to recreate %s: %s", path, exc)
            return False

    async def _investigate(self, delta: ContextDelta, tool_name: str, args: dict) -> None:
        """Run a single read-only tool, then decide whether to notify."""
        if tool_name not in _HEARTBEAT_ALLOWED_TOOLS:
            logger.warning("Heartbeat tried disallowed tool: %s", tool_name)
            if not delta.is_empty:
                await self._notify(delta)
            return

        if self._tools is None:
            if not delta.is_empty:
                await self._notify(delta)
            return

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, self._tools.execute, tool_name, args)
            result_str = str(result)[:1000]
        except Exception as exc:
            logger.warning("Heartbeat tool %s failed: %s", tool_name, exc)
            self._store_tick("FAIL", f"{tool_name}: {exc}")
            if not delta.is_empty:
                await self._notify(delta)
            return

        self._store_tick("INVESTIGATE", f"{tool_name}({args}) → {result_str[:200]}")

        # Second opinion
        follow_up = (
            f"You ran {tool_name}({args}). Result: {result_str}\n\n"
            f"Original context: {delta.summary()}\n"
            f"Decide: INFORM <what to tell user> | NOTHING"
        )
        from .providers import ChatMessage
        try:
            resp = await loop.run_in_executor(
                None, self._provider.chat, [ChatMessage(role="user", content=follow_up)], None
            )
            decision = (resp.content or "").strip()
            if decision.startswith("INFORM"):
                reason = decision[len("INFORM"):].strip()
                self._store_tick("INFORM", f"after {tool_name}: {reason}")
                await self._notify(delta, reason)
            else:
                logger.info("Heartbeat investigated then decided nothing — %s", result_str[:200])
        except Exception as exc:
            logger.warning("Heartbeat second opinion failed: %s", exc)
            if not delta.is_empty:
                await self._notify(delta)

    # ── notification ───────────────────────────────────────────────

    async def _notify(self, delta: ContextDelta, reason: str = "") -> None:
        if self._send_fn is None:
            logger.info("Heartbeat: %s — %s", delta.summary(), reason)
            self._store_tick("NOTIFY (log)", reason or delta.summary())
            return

        notified_any = False
        for title in delta.overdue_tasks:
            fp = f"overdue:{title}"
            if fp in self._notified:
                continue
            self._notified.add(fp)
            notified_any = True
            text = f"⏰ Task overdue: _{title}_"
            if reason:
                text += f"\n{reason}"
            await self._send_fn(text)

        if delta.delegate_pending:
            fp = "delegate_pending"
            if fp not in self._notified:
                self._notified.add(fp)
                notified_any = True
                text = "📋 You have pending tasks."
                if reason:
                    text += f" {reason}"
                await self._send_fn(text)

        for job_id in delta.cron_failures:
            fp = f"cron_fail:{job_id}"
            if fp in self._notified:
                continue
            self._notified.add(fp)
            notified_any = True
            text = f"⚠️ Cron job _{job_id}_ is failing."
            if reason:
                text += f" {reason}"
            await self._send_fn(text)

        if not notified_any and reason:
            logger.info("Heartbeat notify skipped (already notified): %s", reason)

        if delta.git_changes:
            logger.info("Heartbeat: git dirty — %s", delta.git_changes[:200])
        if delta.new_reports:
            logger.info("Heartbeat: new reports — %s", delta.new_reports)

        # TTL cleanup: prune old notified keys every 24h
        if time.time() - getattr(self, '_last_notified_prune', 0) > 86400:
            self._last_notified_prune = time.time()
            self._notified.clear()

    async def _slice_compact_user_model(self) -> None:
        """Every 24h: summarize old entries from USER_MODEL.md via LLM.

        Keeps last 7 sections intact. Older sections condensed to 3-5 bullet
        summary under '## Archived Observations'. (Phase 4)
        """
        if not self._provider:
            return
        import time as _t
        now = _t.time()
        last = getattr(self, '_last_user_model_compact', 0)
        if now - last < 86400:
            return
        self._last_user_model_compact = now
        try:
            vault_path = Path.home() / ".crow_agent" / "USER_MODEL.md"
            if not vault_path.exists():
                return
            content = vault_path.read_text(encoding="utf-8")
            lines = content.split("\n")
            if len(lines) < 20:
                return
            sections: list[list[str]] = [[]]
            for line in lines:
                if line.startswith("## "):
                    sections.append([line])
                else:
                    sections[-1].append(line)
            if len(sections) < 3:
                return
            recent = "\n".join("\n".join(s) for s in sections[-7:])
            old_sections = sections[:-7]
            if not old_sections:
                return
            old_text = "\n\n".join("\n".join(s).strip() for s in old_sections if any(l.strip() for l in s))
            if len(old_text) < 200:
                return
            from .providers import ChatMessage
            prompt = (
                "Summarize the following observations about a user into 3-5 concise "
                "bullet points. These are old entries being compacted. Keep only "
                "what's still relevant and useful.\n\n"
                f"{old_text[:3000]}"
            )
            try:
                resp = self._provider.chat(
                    messages=[ChatMessage(role="user", content=prompt)],
                    max_tokens=500,
                )
                summary = resp.content.strip()
            except Exception:
                logger.debug("USER_MODEL compaction LLM call failed", exc_info=True)
                return
            if not summary:
                return
            new_content = f"## Archived Observations\n{summary}\n\n{recent}"
            vault_path.write_text(new_content.strip() + "\n", encoding="utf-8")
            self._store_tick("COMPACT", f"USER_MODEL: {len(old_sections)} sections condensed")
            logger.info("USER_MODEL.md compacted: %d old sections", len(old_sections))
        except Exception:
            logger.debug("USER_MODEL compaction skipped", exc_info=True)
