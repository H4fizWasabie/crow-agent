"""TDD tests for crew system — scratchpad, plan, execution, merge."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Scratchpad tests ──────────────────────────────────────────────

def test_scratchpad_append_and_query():
    """Workers append ## STEP: blocks, queries find by status."""
    from crow_agent.crew import CrewScratchpad

    sp = CrewScratchpad()
    sp.append_step("research", "researcher", "done", "Found 3 relevant papers about AI agents.")
    sp.append_step("code", "deep-worker", "running", "")
    sp.append_step("review", "code-reviewer", "done", "Code looks good, 2 minor suggestions.")

    # Query done steps
    done = sp.query_done()
    assert len(done) == 2
    assert "research" in done[0]
    assert "review" in done[1]

    # Query by worker
    researcher_steps = sp.query_by_worker("researcher")
    assert len(researcher_steps) == 1
    assert "3 relevant papers" in researcher_steps[0]

    # Raw markdown has correct format
    raw = sp.read_raw()
    assert "## STEP: research | worker: researcher | status: done" in raw
    assert "## END" in raw
    assert "## STEP: code | worker: deep-worker | status: running" in raw


def test_scratchpad_script_query():
    """Simulate worker querying scratchpad via awk script — think-in-code pattern."""
    from crow_agent.crew import CrewScratchpad

    sp = CrewScratchpad()
    sp.append_step("research", "researcher", "done", "Paper 1: AI agents\nPaper 2: Multi-agent")
    sp.append_step("code", "deep-worker", "done", "def main(): pass")
    sp.append_step("review", "code-reviewer", "running", "")

    # Worker queries: awk '/## STEP:.*status: done/,/## END/' scratchpad.md
    import subprocess
    result = subprocess.run(
        ["awk", '/## STEP:.*status: done/,/## END/', sp.path],
        capture_output=True, text=True,
    )
    assert "Paper 1" in result.stdout
    assert "def main" in result.stdout
    assert "review" not in result.stdout  # running, not done


def test_scratchpad_empty():
    """Empty scratchpad: query returns empty list, no crash."""
    from crow_agent.crew import CrewScratchpad

    sp = CrewScratchpad()
    assert sp.query_done() == []
    assert sp.read_raw() == ""


# ── Plan parsing tests ────────────────────────────────────────────

def test_parse_valid_plan():
    """Valid JSON plan with dependency graph parses correctly."""
    from crow_agent.crew import parse_plan

    plan_json = json.dumps({
        "steps": [
            {"id": "research", "worker": "researcher", "task": "Search for AI papers", "depends_on": []},
            {"id": "code", "worker": "deep-worker", "task": "Write prototype", "depends_on": ["research"]},
            {"id": "review", "worker": "code-reviewer", "task": "Review code", "depends_on": ["code"]},
        ]
    })
    plan = parse_plan(plan_json)
    assert plan is not None
    assert len(plan.steps) == 3
    assert plan.steps[1].depends_on == ["research"]


def test_parse_invalid_json_returns_none():
    """Invalid JSON returns None (caller retries or falls back)."""
    from crow_agent.crew import parse_plan

    assert parse_plan("not json") is None
    assert parse_plan('{"steps": [}') is None
    assert parse_plan("") is None


def test_plan_dependency_levels():
    """PlanExecutor groups steps by dependency level."""
    from crow_agent.crew import Plan, PlanStep, get_ready_steps

    plan = Plan(steps=[
        PlanStep(id="a", worker="w1", task="t1", depends_on=[]),
        PlanStep(id="b", worker="w2", task="t2", depends_on=["a"]),
        PlanStep(id="c", worker="w3", task="t3", depends_on=["a"]),
        PlanStep(id="d", worker="w4", task="t4", depends_on=["b", "c"]),
    ])

    # Level 0: a (no dependencies)
    ready = get_ready_steps(plan, completed=set())
    assert [s.id for s in ready] == ["a"]

    # After a done: level 1: b, c
    ready = get_ready_steps(plan, completed={"a"})
    assert [s.id for s in ready] == ["b", "c"]

    # After b, c done: level 2: d
    ready = get_ready_steps(plan, completed={"a", "b", "c"})
    assert [s.id for s in ready] == ["d"]


# ── Provider pool tests ───────────────────────────────────────────

def test_provider_pool_routing():
    """Pool picks correct provider by profile name."""
    from crow_agent.crew import get_worker_provider

    # Mock provider manager with entries as ProviderEntry-like objects
    class MockEntry:
        def __init__(self, name, model):
            self.name = name
            self.model = model

    pm = MagicMock()
    pm.all_entries.return_value = [
        MockEntry("opencode-zen-1", "deepseek-v4-flash-free"),
        MockEntry("opencode-zen-2", "deepseek-v4-flash-free"),
        MockEntry("opencode-zen-3", "mimo-v2.5-free"),
        MockEntry("opencode-zen-4", "big-pickle"),
    ]

    profile_primaries = {
        "researcher": "opencode-zen-1",
        "deep-worker": "opencode-zen-2",
        "code-reviewer": "opencode-zen-3",
    }

    # Patch resolve_provider at the import location inside crew module
    with patch("crow_agent.providers.resolve_provider") as mock_resolve:
        mock_resolve.return_value = MagicMock()
        provider = get_worker_provider("researcher", pm, profile_primaries)
        assert provider is not None
        mock_resolve.assert_called_once()


# ── Merge tests ───────────────────────────────────────────────────

def test_merge_scratchpad_content():
    """Merge extracts all done steps into a synthesis prompt."""
    from crow_agent.crew import CrewScratchpad, build_merge_prompt

    sp = CrewScratchpad()
    sp.append_step("research", "researcher", "done", "AI agents are trending.")
    sp.append_step("code", "deep-worker", "done", "Prototype works.")
    sp.append_step("review", "code-reviewer", "running", "")

    prompt = build_merge_prompt(sp)
    assert "AI agents are trending" in prompt
    assert "Prototype works" in prompt
    assert "running" not in prompt  # only done steps
    assert "synthesize" in prompt.lower() or "merge" in prompt.lower() or "report" in prompt.lower()


# ── Persistent worker memory tests ────────────────────────────────

def test_worker_session_persistence(db):
    """Workers with session_id persist turns to DB and recall history."""
    from crow_agent.agent_profiles import AgentProfile, run_child_task
    from crow_agent.providers import ChatMessage
    from unittest.mock import MagicMock

    # Mock provider that returns a simple response
    mock_provider = MagicMock()
    mock_provider.chat.return_value.content = "Task done."
    mock_provider.chat.return_value.tool_calls = None

    profile = AgentProfile(
        name="test-worker",
        instructions="You are a test worker.",
        tools=["read_file"],
    )

    tools = MagicMock()
    tools.get.return_value = None  # No tool schemas needed

    # First run — no history
    result1 = run_child_task(
        profile, "Task 1: hello", mock_provider, tools,
        session_id="worker:test-worker",
        db_path=":memory:",
    )
    assert "Task done" in result1

    # Second run — should have history from first run
    result2 = run_child_task(
        profile, "Task 2: continue", mock_provider, tools,
        session_id="worker:test-worker",
        db_path=":memory:",
    )
    assert "Task done" in result2

    # Verify turns were persisted
    db.append_turn("worker:test-worker", "user", "Task 1: hello")
    db.append_turn("worker:test-worker", "assistant", "Task done.")
    history = db.history("worker:test-worker", limit=10)
    assert len(history) >= 2
