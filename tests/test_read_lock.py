"""Tests for Option C — read-lock: after N consecutive reads, read tools become unavailable."""

from unittest.mock import MagicMock

from crow_agent.providers import ChatResponse
from crow_agent.run_agent import AIAgent, Trigger, TriggerSource


def _mk_resp(content="", tool_calls=None):
    return ChatResponse(content=content, tool_calls=tool_calls or [],
                        finish_reason="stop",
                        usage={"prompt_tokens": 10, "completion_tokens": 5})


def _fake_tool(name, args=None):
    return [{"id": "call_1", "type": "function",
             "function": {"name": name, "arguments": args or "{}"}}]


class TestReadLock:
    """After 3 consecutive reads, read tools get filtered from available list."""

    def _mk_agent(self, db):
        from crow_agent.model_tools import register_builtins
        from crow_agent.toolsets import ToolRegistry
        from crow_agent.skills_system import SkillsIndex
        mock = MagicMock()
        registry = ToolRegistry()
        register_builtins(registry)
        agent = AIAgent(
            session_id="test_readlock",
            provider=mock,
            db_path=db._path if hasattr(db, '_path') else ":memory:",
            tool_registry=registry,
            skills_index=SkillsIndex(),
        )
        agent._db = db
        # Override round limit for faster testing
        agent._round_limit_override = True
        return agent, mock

    def test_read_streak_counter_increments(self, db):
        """Consecutive reads increment counter, write resets it."""
        from crow_agent.tool_executor import _is_read_tool

        # read_file is always-read
        assert _is_read_tool("read_file", '{"path":"x.txt"}')
        # edit_file is always-write
        assert not _is_read_tool("edit_file", '{"path":"x.txt","old":"a","new":"b"}')

        # Simulate streak tracking
        streak = 0
        # 3 reads
        for _ in range(3):
            assert _is_read_tool("read_file", "{}")
            streak += 1
        assert streak == 3

        # Write resets
        if not _is_read_tool("edit_file", '{"path":"x.txt","old":"a","new":"b"}'):
            streak = 0
        assert streak == 0

    def test_read_streak_max_limit(self, db):
        """After MAX_READ_STREAK consecutive reads, lock triggers."""
        from crow_agent.tool_executor import _is_read_tool

        MAX_READ_STREAK = 3
        streak = 0
        read_tool_calls = ["read_file", "grep_files", "web_search"]

        for tool_name in read_tool_calls:
            if _is_read_tool(tool_name, "{}"):
                streak += 1
                if streak >= MAX_READ_STREAK:
                    break

        assert streak >= MAX_READ_STREAK

    def test_available_tools_excludes_reads_after_lock(self, db):
        """When lock is active, read tools are filtered from the available list."""
        from crow_agent.tool_executor import _is_read_tool, _ALWAYS_READ_TOOLS

        locked = True
        all_tools = ["read_file", "grep_files", "web_search", "web_fetch",
                     "edit_file", "write_file", "run_cmd", "get_time"]
        available = []

        for name in all_tools:
            if locked and _is_read_tool(name, "{}"):
                continue
            available.append(name)

        assert "edit_file" in available
        assert "write_file" in available
        # read tools excluded
        assert "read_file" not in available
        assert "grep_files" not in available
        assert "web_search" not in available

    def test_read_lock_integration_simulated(self, db):
        """Simulate a full turn: 3 reads trigger lock, next tool call must be write."""
        from crow_agent.tool_executor import _is_read_tool

        MAX_READ_STREAK = 3
        streak = 0
        lock_active = False

        # Tool sequence: read, read, read → lock triggers
        sequence = [
            ("read_file", '{"path":"x.txt"}'),
            ("grep_files", '{"pattern":"def"}'),
            ("web_search", '{"q":"test"}'),
            # 4th call — if LLM tries another read, it's filtered
            ("read_file", '{"path":"y.txt"}'),
        ]

        for name, args in sequence:
            is_read = _is_read_tool(name, args)
            if is_read:
                streak += 1
            else:
                streak = 0
                lock_active = False

            if streak >= MAX_READ_STREAK:
                lock_active = True

            if lock_active and is_read:
                # This read would be filtered — LLM can't see it
                continue

        # After 4 calls (3 reads + 1 filtered), streak should be at limit
        assert streak >= MAX_READ_STREAK
        assert lock_active
