"""Built-in tools — coordinator module.

Imports and registers tool domain modules, then prunes dead tools
and registers extensions as lazy-loaded.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

from .toolsets import ToolRegistry
from .paths import PROJECT_ROOT

logger = logging.getLogger("crow_agent.extensions")

# Tools removed (use run_cmd instead):
_DEAD_TOOLS: set[str] = {
    # Duplicates (run_cmd handles these)
    "bash", "exec_async", "pip_install",
    # Process wrappers (run_cmd ps/tail/kill)
    "process_log", "process_poll", "process_list", "process_kill",
    # File search (run_cmd grep/find/ls)
    "grep_files", "list_dir", "glob",
    # Git wrappers (run_cmd git status/diff)
    "git_status",
    # SSH wrapper (run_cmd ssh)
    "ssh_exec",
    # Cron wrappers (run_cmd crontab)
    "cron_list", "cron_create", "cron_remove", "cron_pause", "cron_resume",
    # Task secretary (run_cmd sqlite3 for tasks)
    "create_task", "list_tasks", "complete_task", "snooze_task",
    # Media (generate_image/say/etc never used; ocr_document un-deaded 2026-06-23)
    "extract_pdf_text", "convert_file",
    # Browser (never used, web_fetch covers)
    "browser_fetch", "browser_navigate", "browser_snapshot",
    "browser_click", "browser_type", "browser_press",
    "browser_scroll", "browser_back", "browser_page_text",
    "scrape_page",
    # Meta/planning (unused)
    "begin_plan", "commit_plan", "restore_file",
    "crow_run", "list_tools", "pipe_table",
    # Duplicates of recall
    "retrieve", "semantic_recall",
    # Unused comms
    "post_to_threads",
    # Unused misc
    "get_time", "find",
}

# Tools only available via lazy extensions (moved from core to extension):
_LAZY_TOOLS: set[str] = {
    # Web (lazy extension)
    "web_search", "web_fetch",
    # Agent (lazy extension)
    "spawn_agent", "spawn_team", "delegate_task", "cancel_task", "task_status",
    # Skills (lazy extension)
    "create_skill", "list_skills",
    # Session search (lazy extension)
    "session_search",
}


def register_builtins(
    registry: ToolRegistry,
    spawn_fn: Any | None = None,
    retrieve_fn: Any | None = None,
    task_db: Any | None = None,
    delegate_fn: Any | None = None,
) -> None:
    """Register tools: core 7 always, rest lazy-loaded on trigger match."""

    deps: dict[str, Any] = {
        "task_db": task_db,
        "spawn_fn": spawn_fn,
        "retrieve_fn": retrieve_fn,
        "delegate_fn": delegate_fn,
    }

    # ── Register all tools from existing modules ──
    from . import (
        tools_file, tools_web, tools_comms, tools_media, tools_tasks,
        tools_git, tools_agent, tools_ssh,
        tools_process, skills_system, tools_cron, tools_lsp, tools_mcp,
    )

    tools_file.register_tools(registry)
    tools_web.register_tools(registry)
    tools_comms.register_tools(registry)
    tools_media.register_tools(registry)
    tools_tasks.register_tools(registry, **deps)
    tools_git.register_tools(registry, **deps)
    tools_agent.register_tools(registry, **deps)
    tools_ssh.register_tools(registry)
    skills_system.register_tools(registry)
    tools_process.register_tools(registry)
    tools_cron.register_tools(registry)
    tools_lsp.register_tools(registry)
    tools_mcp.register_tools(registry)

    # ── Register inline tools ──
    @registry.register(
        description="Shell command (bash). USE FOR EVERYTHING: git, grep, find, ssh, sqlite3, python3, pip, ps, ls, date, crontab, process management. One tool for all shell needs."
    )
    def run_cmd(command: str, timeout: int = 30) -> str:
        import subprocess
        if re.search(r'git\s+revert', command):
            return "[BLOCKED] git revert is disabled. To undo a change, write new code with edit_file."
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
            parts = [result.stdout]
            if result.stderr:
                parts.append(f"[stderr]\n{result.stderr}")
            if result.returncode != 0:
                parts.append(f"[exit code: {result.returncode}]")
            return "\n".join(parts).strip()
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout}s"
        except Exception as exc:
            return f"Error executing command: {exc}"

    # ── Prune dead tools ──
    removed = 0
    for name in _DEAD_TOOLS:
        if registry.remove(name):
            removed += 1
    logger.info("Pruned %d dead tools from registry", removed)

    # ── Move lazy tools to extensions (remove from eager, re-register as lazy) ──
    _lazy_snapshots: dict[str, dict[str, Any]] = {}
    for name in _LAZY_TOOLS:
        tool = registry.get(name)
        if tool:
            _lazy_snapshots[name] = {
                "name": tool.name,
                "description": tool.description,
                "fn": tool.fn,
            }
            registry.remove(name)

    # ── Register lazy extensions ──
    def _lazy_web_reg(r: ToolRegistry) -> None:
        for name in ("web_search", "web_fetch"):
            if name in _lazy_snapshots:
                t = _lazy_snapshots[name]
                r.register(description=t["description"])(t["fn"])

    def _lazy_agent_reg(r: ToolRegistry) -> None:
        for name in ("spawn_agent", "spawn_team", "delegate_task", "cancel_task", "task_status"):
            if name in _lazy_snapshots:
                t = _lazy_snapshots[name]
                r.register(description=t["description"])(t["fn"])

    def _lazy_skills_reg(r: ToolRegistry) -> None:
        for name in ("create_skill", "list_skills"):
            if name in _lazy_snapshots:
                t = _lazy_snapshots[name]
                r.register(description=t["description"])(t["fn"])

    def _lazy_session_reg(r: ToolRegistry) -> None:
        if "session_search" in _lazy_snapshots:
            t = _lazy_snapshots["session_search"]
            r.register(description=t["description"])(t["fn"])

    registry.register_lazy("web", r'\b(search|fetch|scrape|browse|research|web|url|look\s*up|lookup|find\s+online)\b', _lazy_web_reg)
    registry.register_lazy("agent", r'\b(spawn|delegate|cancel\s+task|task\s+status|background|crew)\b', _lazy_agent_reg)
    registry.register_lazy("skills", r'\b(create\s+skill|list\s+skill|skills\b)', _lazy_skills_reg)

    # Session search
    registry.register_lazy(
        "session-search",
        r'\b(session\s+search|search\s+(past|history|conversation)|recall|fts5)\b',
        _lazy_session_reg,
    )

    # ── Lazy extensions (external extension dirs) ──
    registry.register_lazy(
        "project",
        r'\b(project\s+(create|focus|decide|task)|new\s+project)\b',
        lambda r: _load_extension(r, "project_memory"),
    )
    registry.register_lazy(
        "youtube",
        r'\b(youtube|transcribe|video|cc|subtitles)\b',
        lambda r: _load_extension(r, "youtube_transcribe"),
    )


def _load_extension(registry: ToolRegistry, ext_name: str) -> None:
    """Load a single extension by name. Best-effort."""
    import importlib
    extensions_dir = PROJECT_ROOT / "extensions"
    module_path = f"extensions.{ext_name}"
    try:
        root_str = str(PROJECT_ROOT)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        if "extensions" not in sys.modules:
            importlib.import_module("extensions")
        mod = importlib.import_module(module_path)
        if hasattr(mod, "register_tools"):
            mod.register_tools(registry)
            logger.info("Extension loaded: %s", ext_name)
    except Exception as exc:
        logger.warning("Extension '%s' failed to load: %s", ext_name, exc)
