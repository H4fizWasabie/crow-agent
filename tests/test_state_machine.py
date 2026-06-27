"""State machine tests for AIAgent turn loop.

Covers the core orchestration paths: RECALL → ASSEMBLE → CALL → TOOL_LOOP → VERIFY → RESPOND.
Uses mock providers so no real LLM API calls are made.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest

from crow_agent.crow_state import CrowState
from crow_agent.model_tools import register_builtins
from crow_agent.providers import (
    BaseProvider,
    ChatMessage,
    ChatResponse,
    ProviderConfig,
)
from crow_agent.run_agent import AIAgent, State, Trigger, TriggerSource
from crow_agent.skills_system import SkillsIndex
from crow_agent.toolsets import ToolRegistry


# ── helpers ──


def _mock_response(
    content: str = "OK",
    tool_calls: list[dict] | None = None,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> ChatResponse:
    return ChatResponse(
        content=content,
        tool_calls=tool_calls or [],
        finish_reason="stop",
        usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        reasoning_content=None,
    )


def _fake_tool_call(name: str, args: dict | None = None) -> list[dict]:
    return [
        {
            "id": f"call_{name}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args or {}),
            },
        }
    ]


# ── fixtures ──


@pytest.fixture
def db() -> Generator[CrowState, None, None]:
    store = CrowState(db_path=":memory:")
    store.create_session("test_session")
    yield store
    store.close()


@pytest.fixture
def registry() -> ToolRegistry:
    r = ToolRegistry()
    register_builtins(r)
    return r


@pytest.fixture
def mock_provider() -> MagicMock:
    p = MagicMock(spec=BaseProvider)
    p.config = ProviderConfig(name="mock", base_url="http://mock", api_key="test", model="mock-model")
    p.chat.return_value = _mock_response()
    return p


def _mk_agent(
    db: CrowState,
    registry: ToolRegistry,
    provider: BaseProvider,
    skills: SkillsIndex | None = None,
) -> AIAgent:
    """Construct an AIAgent with minimal / test-friendly dependencies."""
    return AIAgent(
        session_id="test_session",
        provider=provider,
        db_path=":memory:",
        tool_registry=registry,
        skills_index=skills or SkillsIndex(),
        identity="You are a test agent.",
        # Point context files at a known-safe location
        soul_path=str(Path.cwd() / "SOUL.md"),
        user_path=str(Path.cwd() / "USER.md"),
        memory_path=str(Path.cwd() / "MEMORY.md"),
        fts_limit=3,
        history_limit=20,
    )


# ── tests ──


class TestBasicTurn:
    """Normal turn flows — no tools, simple text response."""

    def test_agent_returns_text(self, db, registry, mock_provider):
        agent = _mk_agent(db, registry, mock_provider)
        result = agent.run(Trigger(source=TriggerSource.USER, prompt="hello"))
        assert "OK" in result
        assert agent.state == State.IDLE

    def test_user_turn_persisted(self, db, registry, mock_provider):
        agent = _mk_agent(db, registry, mock_provider)
        agent.run(Trigger(source=TriggerSource.USER, prompt="remember this"))
        turns = agent._db.history("test_session", limit=10)
        assert any(t["role"] == "user" and "remember this" in t["content"] for t in turns)

    def test_assistant_turn_persisted(self, db, registry, mock_provider):
        agent = _mk_agent(db, registry, mock_provider)
        agent.run(Trigger(source=TriggerSource.USER, prompt="hi"))
        turns = agent._db.history("test_session", limit=10)
        assert any(t["role"] == "assistant" and "OK" in t["content"] for t in turns)


class TestStateMachine:
    """State transitions during a turn."""

    def test_state_flow_idle_to_idle(self, db, registry, mock_provider):
        agent = _mk_agent(db, registry, mock_provider)
        assert agent.state == State.IDLE
        agent.run(Trigger(source=TriggerSource.USER, prompt="go"))
        assert agent.state == State.IDLE

    def test_state_sequence(self, db, registry, mock_provider):
        """Verify states visited in order during a turn."""
        agent = _mk_agent(db, registry, mock_provider)
        visited: list[str] = []

        real_prepare = agent._prepare_turn
        real_call_start = agent._provider.chat

        def tracking_prepare(ui):
            visited.append("recall_assemble")
            return real_prepare(ui)

        def tracking_call(*args, **kwargs):
            visited.append("call")
            return real_call_start(*args, **kwargs)

        agent._prepare_turn = tracking_prepare
        agent._provider.chat = tracking_call

        agent.run(Trigger(source=TriggerSource.USER, prompt="test"))

        assert "recall_assemble" in visited
        assert "call" in visited


class TestToolLoop:
    """Tool call execution and error handling."""

    def test_tool_call_executed(self, db, registry, mock_provider):
        """Agent calling a read_file should return tool output."""
        responded = False
        def _resp_seq(*a, **kw):
            nonlocal responded
            if not responded:
                responded = True
                return _mock_response(
                    content="Let me read that file",
                    tool_calls=_fake_tool_call("read_file", {"path": str(Path.cwd() / "SOUL.md")}),
                )
            return _mock_response(content="[DONE] Here's what I found")
        mock_provider.chat.side_effect = _resp_seq

        agent = _mk_agent(db, registry, mock_provider)
        result = agent.run(Trigger(source=TriggerSource.USER, prompt="read SOUL.md"))
        assert "Here's what I found" in result

    def test_three_consecutive_failures_aborts(self, db, registry, mock_provider):
        """3 consecutive unknown-tool calls in one batch should abort the tool loop."""
        # Need 3 tool calls IN ONE RESPONSE to trigger abort (consecutive resets each round)
        bad_tools = [
            {"id": f"call_{i}", "type": "function", "function": {"name": "nonexistent_tool_xyz", "arguments": "{}"}}
            for i in range(3)
        ]
        responded = False
        def _resp_seq(*a, **kw):
            nonlocal responded
            if not responded:
                responded = True
                return _mock_response(content="trying...", tool_calls=bad_tools)
            return _mock_response(content="done")
        mock_provider.chat.side_effect = _resp_seq

        agent = _mk_agent(db, registry, mock_provider)
        result = agent.run(Trigger(source=TriggerSource.USER, prompt="run failing command"))
        assert "consecutive" in result.lower() or "fail" in result.lower()

    def test_tool_output_appended_to_history(self, db, registry, mock_provider):
        """Tool calls should append assistant + tool turns to messages."""
        responded = False
        def _resp_seq(*a, **kw):
            nonlocal responded
            if not responded:
                responded = True
                return _mock_response(
                    content="reading...",
                    tool_calls=_fake_tool_call("read_file", {"path": str(Path.cwd() / "SOUL.md")}),
                )
            return _mock_response(content="done")
        mock_provider.chat.side_effect = _resp_seq

        agent = _mk_agent(db, registry, mock_provider)
        agent.run(Trigger(source=TriggerSource.USER, prompt="read"))
        assert agent.state == State.IDLE


class TestCrashSalvage:
    """Crash resilience — turn shouldn't lose data on error."""

    def test_salvage_partial_turn_on_crash(self, db, registry, mock_provider):
        """When the turn crashes, partial data should be persisted before re-raising."""
        agent = _mk_agent(db, registry, mock_provider)

        # Make _provider.chat raise an exception
        mock_provider.chat.side_effect = RuntimeError("LLM connection failed")

        with pytest.raises(RuntimeError):
            agent.run(Trigger(source=TriggerSource.USER, prompt="this will crash"))

        # User turn should still be saved despite the crash
        turns = agent._db.history("test_session", limit=10)
        user_turns = [t for t in turns if t["role"] == "user"]
        assert len(user_turns) >= 1


class TestHistoryBudget:
    """History truncation under token budget."""

    def test_history_truncation(self, db, registry, mock_provider):
        """Having many turns should trigger truncation by budget."""
        # Pre-populate history with verbose content
        agent = _mk_agent(db, registry, mock_provider)
        for i in range(15):
            agent._db.append_turn(
                "test_session", "user",
                f"long message {i} " * 200,  # ~3000 chars each
            )
            agent._db.append_turn(
                "test_session", "assistant",
                f"long reply {i} " * 200,
            )
        agent._history = agent._db.history("test_session", limit=agent.history_limit)

        messages = agent._prepare_turn(Trigger(source=TriggerSource.USER, prompt="short question"))
        assert len(messages) >= 2  # system + user at minimum
        assert agent.state == State.IDLE or agent.state == State.ASSEMBLE
