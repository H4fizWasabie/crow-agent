"""
Test that heartbeat routes test failures through ACT decision pipeline,
not the old direct-fix path.
"""
import pytest
from crow_agent.heartbeat_engine import ContextDelta


def test_context_delta_includes_test_failure():
    """ContextDelta must expose test_failure for LLM decision pipeline."""
    delta = ContextDelta()
    assert hasattr(delta, "test_failure")
    assert delta.test_failure == ""

    delta2 = ContextDelta(test_failure="2 tests failed in test_state_machine.py")
    assert "test_state_machine" in delta2.test_failure
    assert "2 tests failed" in delta2.summary()


def test_context_delta_is_empty_respects_test_failure():
    """test_failure should make delta non-empty."""
    delta = ContextDelta(test_failure="tests failing")
    assert not delta.is_empty


def test_context_delta_summary_includes_test_failure():
    """Summary must mention test failures so LLM sees them."""
    delta = ContextDelta(test_failure="FAILED test_smoke_critical.py - AssertionError")
    assert "tests failing" in delta.summary().lower()
