"""Tests for scratchpad + foreman crew monitoring system."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from crow_agent.scratchpad import CrewScratchpadDB
from crow_agent.foreman import Foreman


# ── Scratchpad ──

@pytest.fixture
def pad() -> CrewScratchpadDB:
    """In-memory scratchpad database."""
    return CrewScratchpadDB(":memory:")


def test_scratchpad_write_and_read(pad: CrewScratchpadDB):
    """Write a task, read it back."""
    pad.write_task("run-1", "task-a", "debugger", "running", "Tracing auth middleware")
    tasks = pad.get_active("run-1")
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == "task-a"
    assert tasks[0]["worker"] == "debugger"
    assert tasks[0]["summary"] == "Tracing auth middleware"


def test_scratchpad_update_status(pad: CrewScratchpadDB):
    """Update a task's status and summary."""
    pad.write_task("run-1", "task-a", "debugger", "running", "Starting")
    pad.write_task("run-1", "task-a", "debugger", "done", "Fixed auth bug")
    tasks = pad.get_active("run-1")
    assert len(tasks) == 0  # done = not active


def test_scratchpad_active_only(pad: CrewScratchpadDB):
    """get_active returns only running/pending tasks."""
    pad.write_task("run-1", "t1", "debugger", "done", "Fixed")
    pad.write_task("run-1", "t2", "researcher", "running", "Searching")
    pad.write_task("run-1", "t3", "architect", "pending", "")
    tasks = pad.get_active("run-1")
    assert len(tasks) == 2
    statuses = {t["status"] for t in tasks}
    assert "running" in statuses
    assert "pending" in statuses
    assert "done" not in statuses


def test_scratchpad_get_log(pad: CrewScratchpadDB):
    """get_log returns all tasks (including done/failed)."""
    pad.write_task("run-1", "t1", "debugger", "done", "Fixed")
    pad.write_task("run-1", "t2", "researcher", "failed", "ModuleNotFoundError")
    log = pad.get_log("run-1")
    assert len(log) == 2


def test_scratchpad_latest_summary_only(pad: CrewScratchpadDB):
    """Multiple writes to same task — only latest kept."""
    pad.write_task("run-1", "t1", "debugger", "running", "Step 1")
    pad.write_task("run-1", "t1", "debugger", "running", "Step 2")
    pad.write_task("run-1", "t1", "debugger", "running", "Step 3")
    tasks = pad.get_active("run-1")
    assert len(tasks) == 1
    assert tasks[0]["summary"] == "Step 3"


def test_scratchpad_stuck_detection(pad: CrewScratchpadDB):
    """Task unchanged for >60s is stale."""
    pad.write_task("run-1", "t1", "debugger", "running", "Same thing")
    # Simulate older timestamp by directly updating SQLite
    pad._conn.execute(
        "UPDATE crew_tasks SET ts = ? WHERE run_id = ? AND task_id = ?",
        (time.time() - 120, "run-1", "t1"),
    )
    pad._conn.commit()
    tasks = pad.get_active("run-1")
    assert len(tasks) == 1
    # Task timestamp should be >60s old
    assert tasks[0]["ts"] < time.time() - 60


# ── Foreman ──

def test_foreman_init_noop_without_pad():
    """Foreman with no scratchpad does nothing."""
    fm = Foreman(scratchpad=None)
    assert len(fm.get_updates()) == 0
    fm.tick()
    assert len(fm.get_updates()) == 0  # no crash, just nothing


def test_foreman_tick_no_active_tasks():
    """tick with empty scratchpad produces no updates."""
    pad = CrewScratchpadDB(":memory:")
    fm = Foreman(scratchpad=pad)
    fm.tick()
    assert len(fm.get_updates()) == 0


def test_foreman_drains_updates():
    """get_updates drains the queue after returning."""
    pad = CrewScratchpadDB(":memory:")
    fm = Foreman(scratchpad=pad)
    fm._pending_updates.append({"type": "done", "task_id": "t1"})
    assert len(fm.get_updates()) == 1
    assert len(fm.get_updates()) == 0  # drained


def test_foreman_pending_context_format():
    """context_text formats pending updates for system prompt."""
    fm = Foreman(scratchpad=None)
    fm._pending_updates = [
        {"type": "done", "task_id": "t1", "worker": "debugger", "summary": "Fixed"},
        {"type": "stuck", "task_id": "t2", "worker": "researcher", "summary": "Still looking"},
    ]
    text = fm.context_text()
    assert "[DONE]" in text
    assert "[STUCK]" in text
    assert "debugger" in text
    assert "researcher" in text
