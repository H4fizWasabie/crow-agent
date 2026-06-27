"""Test simplified agent loop: no guardrails, no interceptors, no coaching.
LLM decides when it's done. Text without tools = natural completion.
"""

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


class TestSimplifiedLoop:

    def _mk(self, db):
        from crow_agent.model_tools import register_builtins
        from crow_agent.toolsets import ToolRegistry
        from crow_agent.skills_system import SkillsIndex
        mock = MagicMock()
        registry = ToolRegistry()
        register_builtins(registry)
        agent = AIAgent(
            session_id="test_session",
            provider=mock,
            db_path=db._path if hasattr(db, '_path') else ":memory:",
            tool_registry=registry,
            skills_index=SkillsIndex(),
        )
        agent._db = db
        return agent, mock

    def test_text_only_returns_to_user(self, db):
        agent, mock = self._mk(db)
        mock.chat.return_value = _mk_resp(content="Hello, how can I help?")
        result = agent.run(Trigger(source=TriggerSource.USER, prompt="hi"))
        assert "Hello" in result
        assert mock.chat.call_count == 1

    def test_tools_executed_and_loop_continues(self, db):
        agent, mock = self._mk(db)
        responses = [
            _mk_resp(content="Let me check...", tool_calls=_fake_tool("get_time")),
            _mk_resp(content="[DONE] The time is 3pm."),
        ]
        mock.chat.side_effect = responses.copy()
        result = agent.run(Trigger(source=TriggerSource.USER, prompt="what time is it?"))
        assert "3pm" in result
        assert mock.chat.call_count == 2

    def test_multiple_tool_rounds(self, db):
        agent, mock = self._mk(db)
        responses = [
            _mk_resp(content="Searching...", tool_calls=_fake_tool("web_search", '{"q":"x"}')),
            _mk_resp(content="Reading result...", tool_calls=_fake_tool("web_fetch", '{"u":"x"}')),
            _mk_resp(content="[DONE] Found the answer: 42."),
        ]
        mock.chat.side_effect = responses.copy()
        result = agent.run(Trigger(source=TriggerSource.USER, prompt="search for answer"))
        assert "42" in result
        assert mock.chat.call_count == 3

    def test_text_with_tools_uses_text_as_context(self, db):
        agent, mock = self._mk(db)
        responses = [
            _mk_resp(content="I need to look at the file first",
                     tool_calls=_fake_tool("read_file", '{"path":"test.txt"}')),
            _mk_resp(content="[DONE] File says: hello world."),
        ]
        mock.chat.side_effect = responses.copy()
        result = agent.run(Trigger(source=TriggerSource.USER, prompt="read test.txt"))
        assert "hello world" in result
        assert mock.chat.call_count == 2

    def test_no_anxiety_on_simple_request(self, db):
        agent, mock = self._mk(db)
        mock.chat.return_value = _mk_resp(content="Done.")
        result = agent.run(Trigger(source=TriggerSource.USER, prompt="say done"))
        assert result == "Done."
        for call in mock.chat.call_args_list:
            args, kwargs = call
            # chat is called as chat(messages=..., tools=...)
            msgs = kwargs.get("messages", args[0] if args else [])
            for msg in msgs:
                assert "INTERCEPTED" not in msg.content
                assert "CONSEQUENCE" not in msg.content
                assert "Tool round" not in msg.content
                assert "LOOP DETECTED" not in msg.content.upper()
