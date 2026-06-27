"""Tests for PyMuPDF extract_pdf_text tool."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "test_pymupdf.pdf"


def test_extract_pdf_text_basic():
    """Should extract text from a PDF."""
    import importlib
    from crow_agent.tools_file import register_tools

    class FakeRegistry:
        def __init__(self):
            self.tools = {}

        def register(self, **kwargs):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorator

        def execute(self, name, params):
            return self.tools[name](**params)

    reg = FakeRegistry()
    register_tools(reg)

    result = reg.tools["extract_pdf_text"](path=str(FIXTURE))
    assert "Hello PyMuPDF" in result
    assert "Second line" in result


def test_extract_pdf_text_first_page_only():
    """first_page_only should return only page 1."""
    import importlib
    from crow_agent.tools_file import register_tools

    class FakeRegistry:
        def __init__(self):
            self.tools = {}

        def register(self, **kwargs):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorator

        def execute(self, name, params):
            return self.tools[name](**params)

    reg = FakeRegistry()
    register_tools(reg)

    result = reg.tools["extract_pdf_text"](path=str(FIXTURE), first_page_only=True)
    assert "Hello PyMuPDF" in result


def test_extract_pdf_text_not_found():
    """Missing file returns error."""
    from crow_agent.tools_file import register_tools

    class FakeRegistry:
        def __init__(self):
            self.tools = {}

        def register(self, **kwargs):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorator

        def execute(self, name, params):
            return self.tools[name](**params)

    reg = FakeRegistry()
    register_tools(reg)

    result = reg.tools["extract_pdf_text"](path="/nonexistent/file.pdf")
    assert "not found" in result.lower() or "error" in result.lower()
