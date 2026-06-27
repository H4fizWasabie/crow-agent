"""Tests for project_memory extension."""

from __future__ import annotations

from pathlib import Path

import pytest

from crow_agent.toolsets import ToolRegistry


@pytest.fixture
def pm_registry(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEMORY_VAULT_DIR", str(tmp_path / "memory vault"))

    import sys, importlib
    from crow_agent.paths import PROJECT_ROOT

    root_str = str(PROJECT_ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    for key in list(sys.modules.keys()):
        if key.startswith("extensions.project_memory"):
            del sys.modules[key]
    sys.modules.pop("extensions", None)

    mod = importlib.import_module("extensions.project_memory")
    reg = ToolRegistry()
    mod.register_tools(reg)
    yield reg


def test_project_create(pm_registry, tmp_path):
    """Should create project folder with template files."""
    result = pm_registry.execute("project_create", {
        "name": "test-project",
        "description": "A test project"
    })
    assert "created" in result.lower() or "test-project" in result.lower()

    proj = tmp_path / "memory vault" / "projects" / "test-project"
    assert proj.exists()
    assert (proj / "CONTEXT.md").exists()
    assert (proj / "decisions.md").exists()
    assert (proj / "tasks.md").exists()


def test_project_decide(pm_registry, tmp_path):
    """Should append decision to decisions.md."""
    # Create project first
    pm_registry.execute("project_create", {"name": "test-proj", "description": "Test"})

    result = pm_registry.execute("project_decide", {
        "name": "test-proj",
        "decision": "Use Scrapling for anti-bot"
    })
    assert "appended" in result.lower() or "saved" in result.lower()

    decisions = (tmp_path / "memory vault" / "projects" / "test-proj" / "decisions.md").read_text()
    assert "Scrapling" in decisions


def test_project_focus(pm_registry, tmp_path):
    """Should load project context."""
    pm_registry.execute("project_create", {"name": "test-proj", "description": "Test desc"})

    result = pm_registry.execute("project_focus", {"name": "test-proj"})
    assert "Test desc" in result
    assert "CONTEXT" in result or "context" in result.lower()


def test_project_nonexistent(pm_registry):
    """Focusing on nonexistent project returns helpful error."""
    result = pm_registry.execute("project_focus", {"name": "nonexistent"})
    assert "not found" in result.lower() or "doesn't exist" in result.lower()
