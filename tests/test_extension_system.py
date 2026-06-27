"""Tests for Crow extension system — discovery, loading, failure isolation."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from crow_agent.model_tools import _discover_extensions
from crow_agent.toolsets import ToolRegistry


@pytest.fixture
def temp_extensions_dir():
    """Create a temp directory with extensions/ subdirectory as a Python package."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ext_pkg = root / "extensions"
        ext_pkg.mkdir()
        (ext_pkg / "__init__.py").write_text("")
        yield root


@pytest.fixture
def registry():
    return ToolRegistry()


# ── Empty extensions dir ────────────────────────────────────────────

def test_no_extensions_dir(registry):
    """Missing extensions/ directory should not crash."""
    with patch("crow_agent.model_tools.PROJECT_ROOT", Path("/nonexistent")):
        _discover_extensions(registry)
    # No exception = pass


def test_empty_extensions_dir(registry):
    """Empty extensions/ directory with no sub-packages should not crash."""
    with patch("crow_agent.model_tools.PROJECT_ROOT", Path("/tmp/empty_ext_test")):
        _discover_extensions(registry)
    # No exception = pass


# ── Healthy extension ───────────────────────────────────────────────

def test_healthy_extension_registers_tools(temp_extensions_dir: Path, registry):
    """A valid extension with register_tools() should register its tools."""
    ext = temp_extensions_dir / "extensions" / "healthy_ext"
    ext.mkdir()
    (ext / "__init__.py").write_text("""
def register_tools(registry):
    @registry.register(description="Test tool")
    def hello():
        return "Hello from extension"
""")

    with patch("crow_agent.model_tools.PROJECT_ROOT", temp_extensions_dir):
        sys.modules.pop("extensions", None)
        sys.modules.pop("extensions.healthy_ext", None)
        _discover_extensions(registry)

    assert "hello" in registry._tools
    result = registry.execute("hello", {})
    assert result == "Hello from extension"


# ── Broken extension does NOT crash core ────────────────────────────

def test_broken_extension_no_init(temp_extensions_dir: Path, registry):
    """Extension dir with no __init__.py should be skipped."""
    ext = temp_extensions_dir / "extensions" / "no_init"
    ext.mkdir()
    # No __init__.py

    with patch("crow_agent.model_tools.PROJECT_ROOT", temp_extensions_dir):
        _discover_extensions(registry)
    # No exception = pass


def test_broken_extension_syntax_error(temp_extensions_dir: Path, registry):
    """Extension with syntax error should be skipped, core survives."""
    ext = temp_extensions_dir / "extensions" / "broken_ext"
    ext.mkdir()
    (ext / "__init__.py").write_text("this is not valid python {{{")

    with patch("crow_agent.model_tools.PROJECT_ROOT", temp_extensions_dir):
        sys.modules.pop("extensions", None)
        sys.modules.pop("extensions.broken_ext", None)
        _discover_extensions(registry)
    # No exception = pass


def test_broken_extension_import_error(temp_extensions_dir: Path, registry):
    """Extension with import error should be skipped, core survives."""
    ext = temp_extensions_dir / "extensions" / "import_err_ext"
    ext.mkdir()
    (ext / "__init__.py").write_text("import nonexistent_module_xyz")

    with patch("crow_agent.model_tools.PROJECT_ROOT", temp_extensions_dir):
        sys.modules.pop("extensions", None)
        sys.modules.pop("extensions.import_err_ext", None)
        _discover_extensions(registry)
    # No exception = pass


def test_extension_missing_register_tools(temp_extensions_dir: Path, registry):
    """Extension without register_tools() should be logged but not crash."""
    ext = temp_extensions_dir / "extensions" / "no_register"
    ext.mkdir()
    (ext / "__init__.py").write_text("x = 1  # no register_tools")

    with patch("crow_agent.model_tools.PROJECT_ROOT", temp_extensions_dir):
        sys.modules.pop("extensions", None)
        sys.modules.pop("extensions.no_register", None)
        _discover_extensions(registry)
    # No exception = pass


# ── Multiple extensions ─────────────────────────────────────────────

def test_multiple_extensions(temp_extensions_dir: Path, registry):
    """Multiple healthy extensions should all register."""
    for name in ["ext_a", "ext_b"]:
        ext = temp_extensions_dir / "extensions" / name
        ext.mkdir()
        (ext / "__init__.py").write_text(f"""
def register_tools(registry):
    @registry.register(description="{name}")
    def {name}():
        return "{name} ok"
""")

    with patch("crow_agent.model_tools.PROJECT_ROOT", temp_extensions_dir):
        sys.modules.pop("extensions", None)
        for name in ["ext_a", "ext_b"]:
            sys.modules.pop(f"extensions.{name}", None)
        _discover_extensions(registry)

    assert "ext_a" in registry._tools
    assert "ext_b" in registry._tools
    assert registry.execute("ext_a", {}) == "ext_a ok"
    assert registry.execute("ext_b", {}) == "ext_b ok"


def test_mixed_healthy_and_broken(temp_extensions_dir: Path, registry):
    """One broken extension should not prevent healthy ones from loading."""
    # Healthy
    ext_ok = temp_extensions_dir / "extensions" / "ok_ext"
    ext_ok.mkdir()
    (ext_ok / "__init__.py").write_text("""
def register_tools(registry):
    @registry.register(description="ok")
    def ok_tool():
        return "i survived"
""")

    # Broken
    ext_bad = temp_extensions_dir / "extensions" / "bad_ext"
    ext_bad.mkdir()
    (ext_bad / "__init__.py").write_text("raise RuntimeError('boom')")

    with patch("crow_agent.model_tools.PROJECT_ROOT", temp_extensions_dir):
        sys.modules.pop("extensions", None)
        sys.modules.pop("extensions.ok_ext", None)
        sys.modules.pop("extensions.bad_ext", None)
        _discover_extensions(registry)

    # Healthy one should still register
    assert "ok_tool" in registry._tools
    assert registry.execute("ok_tool", {}) == "i survived"
