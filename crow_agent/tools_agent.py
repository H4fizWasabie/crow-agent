"""Agent tools: memory, spawning, delegation, retrieval."""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Any

from .tools_common import rebuild_vault_index

logger = logging.getLogger("crow_agent.tools")


def _debug_log(event: str, **fields: Any) -> None:
    """Append one JSON line to debug log. Zero framework — grep/jq it later."""
    entry = {"ts": time.time(), "event": event, **fields}
    log_path = Path.home() / ".crow_agent" / "debug.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass  # ponytail: debug log is best-effort, never block a turn


def register_tools(registry: Any, **kwargs: Any) -> None:
    """Register agent tools."""
    spawn_fn = kwargs.get("spawn_fn")
    retrieve_fn = kwargs.get("retrieve_fn")
    delegate_fn = kwargs.get("delegate_fn")

    @registry.register(description="Persist learned knowledge. category='context' for project facts, 'preference' for user traits.")
    def learn(content: str, category: str = "context") -> str:
        from crow_agent.paths import PROJECT_ROOT
        entry = f"- {content}" if not content.startswith("- ") else content
        if category == "preference":
            path = PROJECT_ROOT / "memory vault" / "USER.md"
            section = "## Learned Preferences"
        else:
            path = PROJECT_ROOT / "memory vault" / "log.md"
            section = None
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            # Dedup: skip if exact entry already exists in the section
            if section and section in existing and entry in existing:
                return f"Already learned ({category}): {content}"
            if section and section not in existing:
                existing += f"\n\n{section}\n"
            path.write_text(existing + entry + "\n", encoding="utf-8")
            return f"Learned ({category}): {content}"
        except OSError as exc:
            return f"Error: {exc}"

    @registry.register(
        description="Persist knowledge to memory vault wiki. Creates a page with YAML frontmatter, rebuilds index.md."
    )
    def remember(title: str, content: str, category: str = "entity") -> str:
        from crow_agent.paths import PROJECT_ROOT
        import re
        wiki_dir = PROJECT_ROOT / "memory vault" / "wiki" / "pages"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        filename = re.sub(r"[^a-z0-9-]", "", title.lower().replace(" ", "-"))
        if not filename:
            return "Error: invalid title — use letters and spaces only"
        filepath = wiki_dir / f"{filename}.md"
        if filepath.exists():
            existing = filepath.read_text(encoding="utf-8")
            filepath.write_text(
                existing.rstrip() + f"\n\n## {date.today()} Update\n{content}\n",
                encoding="utf-8",
            )
        else:
            frontmatter = (
                f"---\n"
                f"type: {category}\n"
                f"tags: []\n"
                f"created: {date.today()}\n"
                f"---\n\n"
                f"{content}\n"
            )
            filepath.write_text(frontmatter, encoding="utf-8")
        rebuild_vault_index(wiki_dir)
        log_path = PROJECT_ROOT / "memory vault" / "log.md"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n## {date.today()} — Remembered\n- [{title}](wiki/pages/{filename}.md)\n")
        return f"✅ Remembered as [[{filename}]] — index updated."

    @registry.register(description="Get the current date and time")
    def get_time() -> str:
        from datetime import datetime, timezone
        now = datetime.now().astimezone()
        return now.strftime("%Y-%m-%d %H:%M:%S %Z")

    if retrieve_fn is not None:
        @registry.register(
            description="Retrieve the full original output of a compressed tool result by ID"
        )
        def retrieve(output_id: str) -> str:
            return retrieve_fn(output_id)

    if spawn_fn is not None:
        @registry.register(
            description="List available agent profiles (roles) that can be spawned via spawn_agent."
        )
        def list_agent_profiles() -> str:
            from .crew import _DEFAULT_PROFILE_PRIMARIES
            from .agent_profiles import load_all_profiles
            profiles = load_all_profiles()
            lines = ["Available agent profiles:"]
            for name in sorted(_DEFAULT_PROFILE_PRIMARIES.keys()):
                model = _DEFAULT_PROFILE_PRIMARIES[name]
                prof = profiles.get(name)
                desc = prof.description[:80] if prof and hasattr(prof, "description") else ""
                lines.append(f"  {name} → {model}  {desc}")
            return "\n".join(lines)

        @registry.register(
            description="Spawn a child agent for a subtask using a named team profile"
        )
        def spawn_agent(role: str, task: str) -> str:
            # ponytail: web-reader is internal to web_fetch, not for direct use
            if role == "web-reader":
                return "Error: web-reader has zero tools and cannot browse the web. Use deep-worker for research tasks."
            result = spawn_fn(role, task)
            _debug_log("spawn_agent", role=role, task=task[:200], result_len=len(result) if result else 0)
            return result

        @registry.register(
            description="Run multiple child agents and collect all results. tasks is a JSON array."
        )
        def spawn_team(tasks: str) -> str:
            try:
                items = json.loads(tasks)
            except json.JSONDecodeError as e:
                return f"Error: invalid JSON — {e}"
            if not isinstance(items, list):
                return "Error: tasks must be a JSON array"
            # ponytail: reject web-reader in team spawns
            for item in items:
                if item.get("role") == "web-reader":
                    return "Error: web-reader has zero tools. Use deep-worker for research tasks."

            _debug_log("spawn_team", team_size=len(items), roles=[it.get("role", "") for it in items])

            results: list[str] = []
            for i, item in enumerate(items):
                role = item.get("role", "")
                task = item.get("task", "")
                if not role or not task:
                    results.append(f"[{i}] Error: missing role or task")
                    continue
                result = spawn_fn(role, task)
                results.append(f"[{i}] {role}:\n{result}")

            combined = "\n\n---\n\n".join(results)
            return f"Spawned {len(items)} agent(s):\n\n{combined}"

    if delegate_fn is not None:
        @registry.register(
            description="Delegate a task for autonomous background execution. Returns immediately, result arrives after the current turn."
        )
        def delegate_task(prompt: str, chat_id: int = 0) -> str:
            return delegate_fn(prompt, chat_id)

    if delegate_fn is not None:
        @registry.register(
            description="Cancel a previously delegated task by its ID. Tasks already executing will be marked cancelled (result discarded)."
        )
        def cancel_task(task_id: str) -> str:
            from .task_registry import cancel_task as _cancel
            return "Cancelled." if _cancel(task_id) else f"Task '{task_id}' not found or already done."

        @registry.register(
            description="Check the status of a delegated task by its ID."
        )
        def task_status(task_id: str) -> str:
            from .task_registry import get as _get
            t = _get(task_id)
            if not t:
                return f"Task '{task_id}' not found."
            lines = [
                f"ID: {t.id}",
                f"State: {t.state}",
                f"Prompt: {t.prompt[:200]}",
                f"Retries: {t.retries}/3",
            ]
            if t.result:
                lines.append(f"Result: {t.result[:500]}")
            if t.error:
                lines.append(f"Error: {t.error[:500]}")
            return "\n".join(lines)

    @registry.register(
        description="Search past conversation sessions for context. Uses FTS5 — free, no API cost. Returns matching turns with session_id and content."
    )
    def session_search(query: str, limit: int = 5) -> str:
        from .crow_state import CrowState
        db = CrowState()
        try:
            results = db.search(query, limit=limit)
        except Exception as exc:
            return f"Search error: {exc}"
        finally:
            db.close()
        if not results:
            return f"No results for: {query}"
        lines = []
        for r in results:
            source = r.get("source", "turn")
            session = r.get("session_id", "")[:16]
            role = r.get("role", "user")
            content = r.get("content", "")[:500]
            lines.append(f"[{source}] session={session} role={role}")
            lines.append(content)
            lines.append("")
        return "\n".join(lines)
