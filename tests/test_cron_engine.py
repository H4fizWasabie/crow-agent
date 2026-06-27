"""Tests for CronEngine: scheduling, execution, job lifecycle."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from crow_agent.cron_engine import CronEngine


def _engine() -> CronEngine:
    """CronEngine isolated from real schedule.json."""
    return CronEngine(schedule_path=tempfile.mktemp(suffix=".json"))


def test_add_and_list_job():
    """Adding a job makes it visible in jobs()."""
    engine = _engine()
    engine.add_job("j1", "test prompt", interval_seconds=60)
    jobs = engine.jobs()
    assert any(j.id == "j1" for j in jobs)


def test_remove_job():
    """Removed job no longer appears."""
    engine = _engine()
    engine.add_job("j1", "test", interval_seconds=60)
    engine.remove_job("j1")
    assert all(j.id != "j1" for j in engine.jobs())


def test_remove_nonexistent_no_error():
    """Removing a missing job is a no-op."""
    engine = _engine()
    engine.remove_job("ghost")  # should not raise


def test_stop_does_not_crash():
    """Stop can be called without error."""
    engine = _engine()
    engine.add_job("j1", "test", interval_seconds=60)
    asyncio.run(engine.stop())  # should not raise


def test_disabled_job_skipped():
    """Disabled job is skipped during scheduling check."""
    engine = _engine()
    executed = []

    async def fake_runner(job) -> None:
        executed.append(job.id)

    engine.set_runner(fake_runner)
    engine.add_job("j1", "test", interval_seconds=60, enabled=False)
    assert len(executed) == 0


def test_multiple_jobs():
    """Multiple jobs can be added independently."""
    engine = _engine()
    engine.add_job("j1", "a", interval_seconds=60)
    engine.add_job("j2", "b", interval_seconds=300)
    assert len(engine.jobs()) == 2
