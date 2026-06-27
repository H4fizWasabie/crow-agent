"""Tests for context management — skill unloading + token budget verification."""

from __future__ import annotations

import pytest


# ── Token Budget (already exists, verify it works) ──────────────────

def test_truncate_history_stays_fit():
    from crow_agent.prompt_builder import truncate_history_by_budget, count_tokens

    history = [
        {"role": "user", "content": "x" * 5000},
        {"role": "assistant", "content": "y" * 5000},
    ]
    system_tokens = 5000
    kept, dropped = truncate_history_by_budget(
        history, system_tokens=system_tokens,
        user_input="test", context_budget=50000,
    )
    assert len(dropped) == 0  # fits comfortably


def test_truncate_history_drops_old():
    from crow_agent.prompt_builder import truncate_history_by_budget

    history = [
        {"role": "user", "content": "a" * 200000},
        {"role": "assistant", "content": "b" * 200000},
        {"role": "user", "content": "c" * 200000},
        {"role": "assistant", "content": "d" * 200000},
    ]
    kept, dropped = truncate_history_by_budget(
        history, system_tokens=5000, user_input="test", context_budget=50000,
    )
    assert len(dropped) >= 1  # must drop something


# ── Skill Unloading ─────────────────────────────────────────────────

def test_skill_loaded_with_turn():
    """Skills track when they were last used."""
    from crow_agent.skills_system import _get_skill_usage, _mark_skill_used

    _mark_skill_used("diagnose", turn=5)
    _mark_skill_used("control", turn=0)
    usage = _get_skill_usage()
    assert usage["diagnose"] == 5
    assert usage["control"] == 0


def test_stale_skills_detected():
    """Skills unused for 3+ turns are flagged."""
    from crow_agent.skills_system import _mark_skill_used, _get_stale_skills

    _mark_skill_used("diagnose", turn=7)
    _mark_skill_used("control", turn=2)
    _mark_skill_used("tdd", turn=8)

    stale = _get_stale_skills(current_turn=10, max_idle=2)
    assert "control" in stale
    assert "diagnose" in stale  # last used at 7, now 10, idle=3 > max_idle=2
    assert "tdd" not in stale
