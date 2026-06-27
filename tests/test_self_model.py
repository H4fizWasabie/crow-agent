"""Tests for SelfModel: mood, health, can_act, prompt chunk."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from crow_agent.self_model import SelfModel, _DEFAULT_STATE


@pytest.fixture
def sm() -> Generator[SelfModel, None, None]:
    """SelfModel with temp DB (isolated from disk)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    model = SelfModel(db_path=tmp.name)
    yield model
    model.close()
    Path(tmp.name).unlink(missing_ok=True)


# ── Init ──

def test_init_creates_table(sm: SelfModel):
    """SelfModel creates self_state table on init."""
    row = sm._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='self_state'"
    ).fetchone()
    assert row is not None


def test_init_seeds_default_state(sm: SelfModel):
    """First init inserts default state row."""
    row = sm._conn.execute(
        "SELECT state_json FROM self_state WHERE id = 1"
    ).fetchone()
    assert row is not None
    import json
    state = json.loads(row["state_json"])
    assert "identity" in state
    assert "status" in state
    assert "mood" in state


def test_init_idempotent(sm: SelfModel):
    """Second init on same DB does not duplicate row."""
    # Re-init same DB
    sm2 = SelfModel(db_path=sm._db_path)
    count = sm2._conn.execute("SELECT COUNT(*) FROM self_state").fetchone()[0]
    assert count == 1
    sm2.close()


# ── Snapshot ──

def test_snapshot_returns_all_keys(sm: SelfModel):
    """snapshot() returns all top-level keys from default state."""
    state = sm.snapshot()
    for key in _DEFAULT_STATE:
        assert key in state, f"Missing key: {key}"


def test_snapshot_injects_uptime(sm: SelfModel):
    """snapshot() computes uptime_seconds >= 0."""
    state = sm.snapshot()
    assert state["identity"]["uptime_seconds"] >= 0


def test_snapshot_injects_mood(sm: SelfModel):
    """snapshot() computes mood field."""
    state = sm.snapshot()
    assert state["mood"] in ("sharp", "normal", "degraded")


# ── Update ──

def test_update_shallow(sm: SelfModel):
    """update(path, value) replaces a top-level key."""
    sm.update("identity", {"model_name": "test-model", "provider": "test-prov"})
    state = sm.snapshot()
    assert state["identity"]["model_name"] == "test-model"
    assert state["identity"]["provider"] == "test-prov"


def test_update_nested_dict(sm: SelfModel):
    """update(path, value) merges into nested dicts."""
    sm.update("status.health", {"disk_pct": 85, "ram_pct": 50})
    state = sm.snapshot()
    assert state["status"]["health"]["disk_pct"] == 85
    assert state["status"]["health"]["ram_pct"] == 50


def test_update_nested_creates_intermediates(sm: SelfModel):
    """update(path, value) creates missing intermediate keys."""
    sm.update("foo.bar.baz", {"x": 1})
    state = sm.snapshot()
    assert state["foo"]["bar"]["baz"] == {"x": 1}


def test_update_scalar(sm: SelfModel):
    """update(path, value) with a non-dict replaces the leaf."""
    sm.update("reflection", {"last_insight": "test", "last_insight_score": 5})
    sm.update("reflection.last_insight_score", 0)
    state = sm.snapshot()
    assert state["reflection"]["last_insight_score"] == 0
    # other keys in reflection preserved
    assert state["reflection"]["last_insight"] == "test"


# ── Mood ──

def test_mood_default_normal(sm: SelfModel):
    """Fresh state returns 'normal' mood."""
    assert sm.mood() == "normal"


def test_mood_degraded_from_errors(sm: SelfModel):
    """Error streak >= 3 → degraded."""
    sm.update("status.health", {"errors_streak": 3})
    assert sm.mood() == "degraded"


def test_mood_degraded_from_low_insight(sm: SelfModel):
    """Insight score <= 2 (and > 0) → degraded."""
    sm.update("reflection", {"last_insight_score": 1})
    assert sm.mood() == "degraded"


def test_mood_degraded_from_failed_initiative(sm: SelfModel):
    """Last initiative failure → degraded."""
    sm.update("status.initiatives", {"last_result": "failure"})
    assert sm.mood() == "degraded"


def test_mood_sharp(sm: SelfModel):
    """Zero errors + high insight + success → sharp."""
    sm.update("status.health", {"errors_streak": 0})
    sm.update("reflection", {"last_insight_score": 4})
    sm.update("status.initiatives", {"last_result": "success"})
    assert sm.mood() == "sharp"


def test_mood_sharp_needs_zero_errors(sm: SelfModel):
    """High insight + success but errors > 0 → not sharp."""
    sm.update("status.health", {"errors_streak": 1})
    sm.update("reflection", {"last_insight_score": 5})
    sm.update("status.initiatives", {"last_result": "success"})
    assert sm.mood() == "normal"


def test_mood_degraded_beats_sharp(sm: SelfModel):
    """Degraded signals override sharp signals."""
    sm.update("status.health", {"errors_streak": 3})  # degraded
    sm.update("reflection", {"last_insight_score": 5})   # would be sharp
    sm.update("status.initiatives", {"last_result": "success"})
    assert sm.mood() == "degraded"


# ── can_act ──

def test_can_act_default_true(sm: SelfModel):
    """Fresh state can act."""
    assert sm.can_act() is True


def test_can_act_blocks_disk_full(sm: SelfModel):
    """Disk >= 95% blocks action."""
    sm.update("status.health", {"disk_pct": 95})
    result = sm.can_act()
    assert result is not True
    assert "Disk" in result


def test_can_act_blocks_ram_full(sm: SelfModel):
    """RAM >= 95% blocks action."""
    sm.update("status.health", {"ram_pct": 96})
    result = sm.can_act()
    assert result is not True
    assert "RAM" in result


def test_can_act_blocks_error_streak(sm: SelfModel):
    """Error streak >= 5 blocks action."""
    sm.update("status.health", {"errors_streak": 5})
    result = sm.can_act()
    assert result is not True
    assert "Error" in result


def test_can_act_allows_below_thresholds(sm: SelfModel):
    """Below-threshold values allow action."""
    sm.update("status.health", {"disk_pct": 90, "ram_pct": 90, "errors_streak": 4})
    assert sm.can_act() is True


# ── to_prompt_chunk ──

def test_to_prompt_chunk_has_header(sm: SelfModel):
    """to_prompt_chunk() starts with '## Self Status'."""
    chunk = sm.to_prompt_chunk()
    assert "## Self Status" in chunk


def test_to_prompt_chunk_has_identity(sm: SelfModel):
    """to_prompt_chunk() includes model/provider info."""
    sm.update("identity", {"model_name": "test-model", "provider": "test-prov", "context_window": 131072})
    chunk = sm.to_prompt_chunk()
    assert "test-model" in chunk
    assert "131K ctx" in chunk


def test_to_prompt_chunk_includes_health(sm: SelfModel):
    """to_prompt_chunk() shows disk/RAM/errors."""
    sm.update("status.health", {"disk_pct": 42, "ram_pct": 63, "errors_streak": 2})
    chunk = sm.to_prompt_chunk()
    assert "Disk 42%" in chunk
    assert "RAM 63%" in chunk
    assert "Errors 2" in chunk


def test_to_prompt_chunk_no_errors_when_zero(sm: SelfModel):
    """to_prompt_chunk() omits errors when streak is 0."""
    sm.update("status.health", {"errors_streak": 0})
    chunk = sm.to_prompt_chunk()
    assert "Errors" not in chunk


def test_to_prompt_chunk_shows_mood_when_not_normal(sm: SelfModel):
    """to_prompt_chunk() includes mood for non-normal states."""
    sm.update("status.health", {"errors_streak": 3})
    chunk = sm.to_prompt_chunk()
    assert "Mood: degraded" in chunk


def test_to_prompt_chunk_hides_mood_when_normal(sm: SelfModel):
    """to_prompt_chunk() omits mood when normal."""
    chunk = sm.to_prompt_chunk()
    assert "Mood:" not in chunk


def test_to_prompt_chunk_shows_reflection(sm: SelfModel):
    """to_prompt_chunk() includes scored reflections."""
    sm.update("reflection", {"last_insight": "User prefers dark mode", "last_insight_score": 4})
    chunk = sm.to_prompt_chunk()
    assert "dark mode" in chunk
    assert "4/5" in chunk


def test_to_prompt_chunk_skips_low_score_reflection(sm: SelfModel):
    """to_prompt_chunk() skips reflections with score < 3."""
    sm.update("reflection", {"last_insight": "meh", "last_insight_score": 2})
    chunk = sm.to_prompt_chunk()
    assert "meh" not in chunk


def test_to_prompt_chunk_shows_task(sm: SelfModel):
    """to_prompt_chunk() shows current task when set."""
    sm.update("context", {"current_task": "Implement self-model"})
    chunk = sm.to_prompt_chunk()
    assert "Implement self-model" in chunk


# ── Thread safety ──

def test_concurrent_updates_no_crash(sm: SelfModel):
    """Multiple updates from threads should not crash."""
    import threading

    def update_health(i: int) -> None:
        sm.update("status.health", {"disk_pct": i})

    threads = [threading.Thread(target=update_health, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    state = sm.snapshot()
    assert state["status"]["health"]["disk_pct"] in range(10)


# ── Schema migration ──

def test_migration_adds_new_keys(sm: SelfModel):
    """If a key is added to _DEFAULT_STATE later, existing rows get it."""
    # Simulate: manually remove a key from the stored JSON
    import json
    row = sm._conn.execute("SELECT state_json FROM self_state WHERE id = 1").fetchone()
    state = json.loads(row["state_json"])
    del state["connections"]
    sm._conn.execute("UPDATE self_state SET state_json = ? WHERE id = 1", (json.dumps(state),))
    sm._conn.commit()

    # Re-init should restore the key
    sm2 = SelfModel(db_path=sm._db_path)
    state2 = sm2.snapshot()
    assert "connections" in state2
    assert state2["connections"] == _DEFAULT_STATE["connections"]
    sm2.close()
