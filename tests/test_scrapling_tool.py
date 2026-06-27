"""Tests for Scrapling scrape_page tool — test fallback path."""

from __future__ import annotations

from unittest.mock import patch

import pytest


def test_scrape_page_fallback():
    """When Scrapling not installed, falls back to httpx + trafilatura."""
    import importlib
    from crow_agent.tools_web import register_tools

    class FakeRegistry:
        def __init__(self):
            self.tools = {}

        def register(self, **kwargs):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorator

    reg = FakeRegistry()
    register_tools(reg)

    # Scrapling imports will fail, so it hits the ImportError fallback
    # which calls _httpx_get then _extract_text
    with patch("crow_agent.tools_web._httpx_get", return_value="<html><body>Hello fallback</body></html>"):
        result = reg.tools["scrape_page"](url="https://example.com")
        assert "Hello fallback" in result


def test_scrape_page_requires_url():
    """Missing URL returns error."""
    from crow_agent.tools_web import register_tools

    class FakeRegistry:
        def __init__(self):
            self.tools = {}

        def register(self, **kwargs):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorator

    reg = FakeRegistry()
    register_tools(reg)

    result = reg.tools["scrape_page"](url="")
    assert "error" in result.lower()
