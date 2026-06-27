"""Test that autonomous health/code-check notifications route to channel, not DM."""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock


class TestSendChannelRouting:
    """_send_channel routes autonomous notifications to Crow Log, not DM."""

    def test_send_channel_uses_crow_log_fn(self):
        """When crow_log_fn is available, _send_channel uses it."""
        from crow_agent.heartbeat_engine import HeartbeatEngine

        mock_crow_log = AsyncMock()
        mock_send = AsyncMock()
        engine = HeartbeatEngine(
            send_fn=mock_send,
            crow_log_fn=mock_crow_log,
        )

        asyncio.run(engine._send_channel("Test message"))

        mock_crow_log.assert_called_once()
        assert "Test message" in mock_crow_log.call_args[0][0]
        mock_send.assert_not_called()

    def test_send_channel_falls_back_to_send_fn(self):
        """When crow_log_fn is None, _send_channel falls back to _send_fn."""
        from crow_agent.heartbeat_engine import HeartbeatEngine

        mock_send = AsyncMock()
        engine = HeartbeatEngine(
            send_fn=mock_send,
            crow_log_fn=None,
        )

        asyncio.run(engine._send_channel("Fallback message"))
        mock_send.assert_called_once_with("Fallback message")


class TestCronFailureRouting:
    """Cron failure notifications route to channel, not DM."""

    def test_cron_failures_go_to_channel(self):
        """_notify sends cron failures via _send_channel."""
        from crow_agent.heartbeat_engine import HeartbeatEngine, ContextDelta

        mock_send = AsyncMock()
        mock_crow_log = AsyncMock()
        engine = HeartbeatEngine(
            send_fn=mock_send,
            crow_log_fn=mock_crow_log,
        )
        engine._notified.clear()

        delta = ContextDelta(cron_failures=["job_test"])
        asyncio.run(engine._notify(delta))

        # Cron failure should go to channel (crow_log_fn), not DM
        mock_crow_log.assert_called_once()
        assert "job_test" in mock_crow_log.call_args[0][0]
        # DM calls should NOT include cron failure
        cron_in_dm = any(
            "job_test" in str(call) for call in mock_send.call_args_list
        )
        assert not cron_in_dm

    def test_overdue_tasks_still_go_to_dm(self):
        """Overdue task notifications still go to DM (user-facing)."""
        from crow_agent.heartbeat_engine import HeartbeatEngine, ContextDelta

        mock_send = AsyncMock()
        mock_crow_log = AsyncMock()
        engine = HeartbeatEngine(
            send_fn=mock_send,
            crow_log_fn=mock_crow_log,
        )
        engine._notified.clear()

        delta = ContextDelta(overdue_tasks=["Submit report"])
        asyncio.run(engine._notify(delta))

        # Overdue tasks should go to DM
        mock_send.assert_called()
        assert "Submit report" in mock_send.call_args[0][0]


class TestAutoCodeCheckChannelRouting:
    """Code check notifications (_auto_code_check) route to channel."""

    def test_send_channel_method_exists(self):
        """_send_channel is a method on HeartbeatEngine."""
        from crow_agent.heartbeat_engine import HeartbeatEngine
        engine = HeartbeatEngine()
        assert hasattr(engine, '_send_channel')
        assert callable(engine._send_channel)
