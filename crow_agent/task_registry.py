"""Thread-safe queue for autonomous background task delegation.

Persistence: tasks survive restart via ~/.crow_agent/tasks.json.
Retry: failed tasks auto-retry up to 2 times.
Cancel: pending tasks can be cancelled; executing tasks marked cancelled (result discarded).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("crow_agent.tasks")

_task_lock = threading.Lock()
_SAVE_PATH = Path.home() / ".crow_agent" / "tasks.json"


# Context var: set before each agent.run() so delegate_fn knows the chat_id
_current_chat_id: ContextVar[int] = ContextVar("_current_chat_id", default=0)


def set_chat_id(chat_id: int) -> None:
    _current_chat_id.set(chat_id)


def get_chat_id() -> int:
    return _current_chat_id.get()


@dataclass
class PendingTask:
    id: str
    prompt: str
    chat_id: int
    profile_name: str = "deep-worker"
    state: str = "pending"
    result: str = ""
    error: str = ""
    retries: int = 0
    completed_at: float = 0.0  # ponytail: for auto-prune stale tasks


_pending: queue.Queue[PendingTask] = queue.Queue(maxsize=100)  # ponytail: bound to prevent memory leak
_tasks: dict[str, PendingTask] = {}


# ── persistence ───────────────────────────────────────────────────


def _prune_stale() -> int:
    """Remove 'done' tasks older than 24 hours. Returns count removed."""
    import time
    now = time.time()
    removed = 0
    with _task_lock:
        stale = [
            tid for tid, t in _tasks.items()
            if t.state == "done" and t.chat_id == 0  # only auto-generated tasks
            and t.completed_at > 0 and (now - t.completed_at) > 86400
        ]
        for tid in stale:
            del _tasks[tid]
            removed += 1
        if removed:
            _save_tasks()
            logger.info("Pruned %d stale done tasks", removed)
    return removed


def _save_tasks() -> None:
    """Persist all tasks to JSON."""
    try:
        _SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "tasks": {tid: task.__dict__ for tid, task in _tasks.items()},
            "queue_order": [t.id for t in list(_pending.queue)],
        }
        _SAVE_PATH.write_text(json.dumps(data, indent=2))
    except OSError as exc:
        logger.warning("Failed to save tasks: %s", exc)


def _load_tasks() -> None:
    """Load persisted tasks and rebuild the queue."""
    try:
        if not _SAVE_PATH.exists():
            return
        data = json.loads(_SAVE_PATH.read_text())
        for tid, raw in data.get("tasks", {}).items():
            task = PendingTask(**raw)
            _tasks[tid] = task
        # Rebuild queue in order, skipping non-pending tasks
        for tid in data.get("queue_order", []):
            task = _tasks.get(tid)
            if task and task.state == "pending":
                try:
                    _pending.put(task, block=False)
                except queue.Full:
                    pass  # queue full at startup — task lost, will be re-created
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("Failed to load tasks: %s", exc)


# Called once at module load
_load_tasks()


# ── task lifecycle ────────────────────────────────────────────────


def enqueue(prompt: str, chat_id: int | None = None, profile: str = "deep-worker") -> str:
    """Add a task to the background queue. Returns task ID."""
    with _task_lock:
        if chat_id is None:
            chat_id = get_chat_id()
            if chat_id == 0:
                logger.warning("enqueue called without set_chat_id() — result will be lost")
        task_id = uuid.uuid4().hex[:8]
        task = PendingTask(
            id=task_id, prompt=prompt, chat_id=chat_id, profile_name=profile
        )
        _tasks[task_id] = task
        try:
            _pending.put(task, block=False)
        except queue.Full:
            del _tasks[task_id]
            logger.warning("Task queue full (max 100) — dropping task: %s", prompt[:100])
            return ""
        _save_tasks()
        return task_id


def dequeue() -> PendingTask | None:
    """Pop the next pending task (non-blocking)."""
    with _task_lock:
        try:
            task = _pending.get(block=False)
            _save_tasks()
            return task
        except queue.Empty:
            return None


def get(task_id: str) -> PendingTask | None:
    """Look up a task by ID."""
    return _tasks.get(task_id)


def update_state(task_id: str, state: str) -> None:
    with _task_lock:
        if task_id in _tasks:
            _tasks[task_id].state = state
            _save_tasks()


def update_result(task_id: str, result: str) -> None:
    import time
    with _task_lock:
        if task_id in _tasks:
            _tasks[task_id].result = result
            _tasks[task_id].state = "done"
            _tasks[task_id].completed_at = time.time()  # ponytail: for auto-prune
            _save_tasks()


def update_error(task_id: str, error: str) -> None:
    import time
    with _task_lock:
        if task_id in _tasks:
            _tasks[task_id].error = error
            _tasks[task_id].state = "failed"
            _tasks[task_id].completed_at = time.time()  # ponytail: for auto-prune
            _save_tasks()


def has_pending() -> bool:
    return not _pending.empty()


# ── cancellation ──────────────────────────────────────────────────


def cancel_task(task_id: str) -> bool:
    """Cancel a task. Returns True if task was found."""
    with _task_lock:
        task = _tasks.get(task_id)
        if not task:
            return False
        if task.state in ("done", "failed", "cancelled"):
            return False
        task.state = "cancelled"
        task.error = "Cancelled by user"
        _save_tasks()
        return True


# ── execution ─────────────────────────────────────────────────────


async def drain_and_execute(
    deliver: Callable[[str, str, str | None], Awaitable[None]],
    *,
    chat_id: int | None = None,  # None = drain all, int = drain only for this chat
    background: bool = False,
) -> None:
    """Execute all pending delegated tasks using the shared execution path."""
    from .agent_profiles import load_all_profiles, run_child_task
    from .providers import resolve_provider
    from .provider_manager import ProviderManager
    from .toolsets import ToolRegistry
    from .model_tools import register_builtins

    profiles = load_all_profiles()
    pm = ProviderManager()

    def _run_inner(task_id: str, prompt: str, profile_name: str) -> None:
        task = get(task_id)
        if task and task.state == "cancelled":
            return  # cancelled before execution started
        profile = profiles.get(profile_name)
        if not profile:
            update_error(task_id, f"Unknown profile '{profile_name}'")
            return
        provider_name = profile.model or "opencode-go"
        try:
            provider = resolve_provider(provider_name, provider_manager=pm)
        except Exception:
            try:
                provider = resolve_provider("opencode-zen", provider_manager=pm)
            except Exception as exc:
                update_error(task_id, str(exc))
                return
        update_state(task_id, "executing")
        try:
            task_tools = ToolRegistry()
            register_builtins(task_tools)
            result = run_child_task(profile, prompt, provider, task_tools)
            update_result(task_id, result)
        except Exception as exc:
            update_error(task_id, str(exc))

    async def _execute(task: Any) -> None:
        # Skip cancelled tasks
        t = get(task.id)
        if t and t.state == "cancelled":
            return

        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(
                    None, _run_inner, task.id, task.prompt, task.profile_name
                ),
                timeout=300,
            )
        except asyncio.TimeoutError:
            update_error(task.id, "Task timed out after 300s")
            t = get(task.id)
            if t and t.state == "failed" and t.retries < 2:
                t.retries += 1
                t.state = "pending"
                t.error = ""
                _pending.put(t, block=False)
                _save_tasks()
                logger.info("Retrying task %s (attempt %d/3)", task.id[:8], t.retries + 1)
                return
            await deliver(t.id, "", "Timed out after 300s")
            return

        t = get(task.id)
        if not t:
            return
        if t.state == "failed" and t.retries < 2:
            t.retries += 1
            t.state = "pending"
            t.error = ""
            try:
                _pending.put(t, block=False)
            except queue.Full:
                logger.warning("Cannot retry task %s — queue full", task.id[:8])
                await deliver(t.id, "", "Queue full — task dropped")
            _save_tasks()
            logger.info("Retrying task %s (attempt %d/3)", task.id[:8], t.retries + 1)
            return
        if t.state == "done":
            await deliver(t.id, t.result, None)
        elif t.state == "failed":
            await deliver(t.id, "", t.error)
        elif t.state == "cancelled":
            pass  # cancelled during execution — discard
        else:
            await deliver(t.id, "", "Task completed with unknown state")

    while task := dequeue():
        if background:
            asyncio.create_task(_execute(task))
        else:
            await _execute(task)
