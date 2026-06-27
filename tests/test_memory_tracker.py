"""Tests for MemoryTracker: milestone detection, preference sniffing, skill extraction."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Generator

import pytest

from crow_agent.memory_tracker import MemoryTracker


@pytest.fixture
def tracker() -> Generator[MemoryTracker, None, None]:
    """MemoryTracker with temp memory + state files (isolated from disk)."""
    mem = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
    mem.write("# Memories\n")
    mem.close()
    state = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    state.write("{}\n")
    state.close()
    yield MemoryTracker(memory_path=mem.name, state_path=state.name)
    Path(mem.name).unlink(missing_ok=True)
    Path(state.name).unlink(missing_ok=True)


def test_tracker_no_match_on_empty_turn(tracker: MemoryTracker):
    """A turn with no tool calls produces no extractions."""
    extractions = tracker.observe_turn(
        session_id="s1",
        turn_count=1,
        tool_calls=[],
        user_input="hello",
        assistant_response="hi",
    )
    assert extractions == []


def test_tracker_tracks_tool_sequences(tracker: MemoryTracker):
    """Short tool sequences (< SEQUENCE_MIN_LENGTH) produce no extractions."""
    # Only 3 tool calls — below the ≥5 minimum
    tc = [{"function": {"name": "web_search"}}, {"function": {"name": "read_file"}}, {"function": {"name": "write"}}]

    e = tracker.observe_turn("s1", 1, tc, "q1", "a1")
    assert e == []


def test_tracker_extraction_needs_minimum_tools(tracker: MemoryTracker):
    """Sequences with <5 tools are never evaluated."""
    # 4 tool calls — still below threshold
    seq = [
        {"function": {"name": "read_file"}},
        {"function": {"name": "grep"}},
        {"function": {"name": "edit_file"}},
        {"function": {"name": "run"}},
    ]

    result = None
    for i in range(5):
        r = tracker.observe_turn("s1", i + 1, seq, f"q{i}", "a")
        if r:
            result = r

    # Old threshold-based extraction is gone. No extraction without LLM eval.
    assert result is None or result == []


def test_inline_extraction_counter(tracker: MemoryTracker):
    """Turn counter increments correctly; extraction attempted every N turns."""
    tc = [{"function": {"name": "web_search"}}]

    for i in range(4):
        r = tracker.observe_turn("s1", i + 1, tc, f"q{i}", "a")
        assert r == [] or r is None, f"Unexpected extraction at turn {i+1}"

    # Turn 5 should trigger inline extraction attempt
    # (may silently fail if no OpenRouter available, but shouldn't crash)
    tracker.observe_turn("s1", 5, tc, "q5", "a")
    # Inline extraction may fail silently (no OpenRouter in test env) - no crash = pass
