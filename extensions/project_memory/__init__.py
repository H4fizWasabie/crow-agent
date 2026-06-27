"""Project Memory extension — manage project folders in memory vault.

Each project is a folder in memory vault/projects/ with:
    CONTEXT.md   — living document (goals, current state, architecture, links)
    decisions.md — append-only decision log (timestamped, immutable)
    tasks.md     — task tracker with status

Tools:
    project_create(name, description)   — create new project folder
    project_focus(name)                 — load all context for discussion
    project_decide(name, decision)      — append to decisions.md
    project_task(name, task, status)    — update tasks.md
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from crow_agent.toolsets import ToolRegistry

logger = logging.getLogger(__name__)

PROJECTS_DIR = "projects"


def _projects_root() -> Path:
    vault = os.environ.get("MEMORY_VAULT_DIR", "memory vault")
    return Path(vault) / PROJECTS_DIR


def _project_dir(name: str) -> Path:
    safe = name.strip().lower().replace(" ", "-")
    return _projects_root() / safe


def _init_project(name: str, description: str) -> None:
    proj = _project_dir(name)
    proj.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not (proj / "CONTEXT.md").exists():
        (proj / "CONTEXT.md").write_text(
            f"# {name}\n\n"
            f"## Summary\n{description}\n\n"
            f"## Current State\nNot started.\n\n"
            f"## Architecture\nTBD.\n\n"
            f"## Links\n- Created: {now}\n"
        )

    if not (proj / "decisions.md").exists():
        (proj / "decisions.md").write_text(
            f"# {name} — Decisions\n\n"
            f"## {now} — Project created: {description}\n"
        )

    if not (proj / "tasks.md").exists():
        (proj / "tasks.md").write_text(
            f"# {name} — Tasks\n\n"
            f"## Backlog\n- [ ] Define requirements\n"
        )


def register_tools(registry: ToolRegistry) -> None:
    @registry.register(
        description="Create a new project folder in memory vault. Initializes CONTEXT.md, decisions.md, and tasks.md. Use when starting a new project discussion."
    )
    def project_create(name: str = "", description: str = "") -> str:
        if not name:
            return "Error: project name required"
        _init_project(name, description)
        return (
            f"✅ Project **{name}** created.\n"
            f"- `CONTEXT.md` — goals, current state, architecture\n"
            f"- `decisions.md` — decision log (append-only)\n"
            f"- `tasks.md` — task tracker\n\n"
            f"Crow can now use `project_focus(\"{name}\")` to load it."
        )

    @registry.register(
        description="Load a project's full context (CONTEXT.md + decisions.md + tasks.md) for discussion. Use when user says 'focus on X' or 'let's talk about X project'."
    )
    def project_focus(name: str = "") -> str:
        if not name:
            return "Error: project name required"
        proj = _project_dir(name)
        if not proj.exists():
            return (
                f"Project **{name}** doesn't exist.\n"
                f"Create it with `project_create(name=\"{name}\", description=\"...\")`"
            )
        parts = []
        for fname, label in [("CONTEXT.md", "Context"), ("decisions.md", "Decisions"), ("tasks.md", "Tasks")]:
            fpath = proj / fname
            if fpath.exists():
                parts.append(f"## {label}\n{fpath.read_text()}")
        return f"# Project: {name}\n\n" + "\n\n".join(parts)

    @registry.register(
        description="Record a decision to the project's decisions.md (append-only, timestamped). Use when user says 'note this down' or 'record this decision'."
    )
    def project_decide(name: str = "", decision: str = "") -> str:
        if not name or not decision:
            return "Error: project name and decision required"
        proj = _project_dir(name)
        if not proj.exists():
            return f"Project **{name}** doesn't exist."
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        entry = f"\n## {stamp} — {decision}\n"
        dfile = proj / "decisions.md"
        with open(dfile, "a") as f:
            f.write(entry)
        return f"📝 Decision appended to **{name}**/decisions.md: {decision}"

    @registry.register(
        description="Update a task in the project's tasks.md. Status can be 'todo', 'doing', or 'done'. Use when managing project tasks."
    )
    def project_task(name: str = "", task: str = "", status: str = "todo") -> str:
        if not name or not task:
            return "Error: project name and task required"
        proj = _project_dir(name)
        if not proj.exists():
            return f"Project **{name}** doesn't exist."
        tfile = proj / "tasks.md"
        marker = {"todo": "[ ]", "doing": "[→]", "done": "[x]"}.get(status, "[ ]")
        entry = f"- {marker} {task}\n"
        with open(tfile, "a") as f:
            f.write(entry)
        return f"✅ Task added to **{name}**/tasks.md: {marker} {task}"
