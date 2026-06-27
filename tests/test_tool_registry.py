"""Tests for ToolRegistry: registration, execution, schema generation."""

from __future__ import annotations

import pytest

from crow_agent.toolsets import ToolRegistry


def test_registry_register_and_execute():
    """Register a tool, execute it, get result back."""
    registry = ToolRegistry()

    @registry.register("greet", "Say hello to someone")
    def greet(name: str = "world") -> str:
        return f"Hello, {name}!"

    result = registry.execute("greet", {"name": "Crow"})
    assert result == "Hello, Crow!"


def test_registry_unknown_tool():
    """Executing an unregistered tool raises KeyError."""
    registry = ToolRegistry()
    with pytest.raises(KeyError):
        registry.execute("nope", {})


def test_registry_all_schemas():
    """all_schemas() returns valid OpenAI-style tool schemas."""
    registry = ToolRegistry()

    @registry.register("ping", "Simple ping")
    def ping() -> str:
        return "pong"

    schemas = registry.all_schemas()
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "ping"


def test_registry_execute_default_args():
    """Tool with defaults works when arg omitted."""
    registry = ToolRegistry()

    @registry.register("echo", "Echo a message")
    def echo(msg: str = "hello") -> str:
        return msg

    assert registry.execute("echo", {}) == "hello"
    assert registry.execute("echo", {"msg": "custom"}) == "custom"


def test_registry_error_propagation():
    """Exceptions in tool functions propagate through execute."""
    registry = ToolRegistry()

    @registry.register("fail", "Always fails")
    def fail() -> str:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        registry.execute("fail", {})
