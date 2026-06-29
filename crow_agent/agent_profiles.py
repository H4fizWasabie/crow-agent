"""Agent profile loader — reads team profiles from `team/` directory.

Each profile is a markdown file with YAML front-matter:

```yaml
---
name: code-reviewer
description: Reviews code for bugs and security issues
model: opencode-go
tools:
  - read_file
  - grep_files
  - run_cmd
---
Body text = system instructions for the child agent.
```
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from .paths import PROJECT_ROOT
from .providers import ChatMessage, ChatResponse
from .run_agent import execute_tool_call

logger = logging.getLogger("crow_agent.team")

DEFAULT_TEAM_DIR = Path(os.environ.get("CROWD_TEAM_DIR", str(PROJECT_ROOT / "team")))

# Tools that child agents are never allowed to use
# Prevents spawn-of-spawn chains and unrestricted delegation
_RESTRICTED_CHILD_TOOLS = frozenset({"delegate_task", "spawn_agent", "spawn_team"})


class AgentProfile:
    """A reusable agent role definition."""

    def __init__(
        self,
        name: str,
        description: str = "",
        model: str | None = None,
        tools: list[str] | None = None,
        instructions: str = "",
        provider_name: str | None = None,
        max_depth: int = 10,
    ) -> None:
        self.name = name
        self.description = description
        self.model = model
        self.tools = tools or []
        self.instructions = instructions
        self.provider_name = provider_name
        self.max_depth = max_depth


def _parse_front_matter(text: str, source: str = "") -> tuple[dict[str, Any], str]:
    """Split YAML front-matter from body. Returns (front_matter, body)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break

    if end == -1:
        return {}, text

    raw_yaml = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:]).strip()
    try:
        fm = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        logger.warning("Profile parse error in %s: %s", source, exc)
        return {}, body
    return (fm if isinstance(fm, dict) else {}), body


def load_profile(path: str | Path) -> AgentProfile | None:
    """Load a single profile from a .md file. Returns None on error."""
    p = Path(path)
    if not p.exists():
        logger.warning("Profile file not found: %s", p)
        return None

    text = p.read_text(encoding="utf-8")
    fm, body = _parse_front_matter(text, source=str(p))

    name = fm.get("name", p.stem)
    if not name:
        logger.warning("Profile %s has no name — skipping", p)
        return None

    return AgentProfile(
        name=name,
        description=fm.get("description", ""),
        model=fm.get("model"),
        tools=fm.get("tools", []),
        instructions=body,
        provider_name=fm.get("provider_name"),
        max_depth=fm.get("max_depth", 10),
    )


def load_all_profiles(team_dir: str | Path | None = None) -> dict[str, AgentProfile]:
    """Scan `team_dir` for .md files and load all profiles. Returns {name: profile}."""
    tdir = Path(team_dir) if team_dir else DEFAULT_TEAM_DIR
    if not tdir.exists():
        return {}

    profiles: dict[str, AgentProfile] = {}
    for fpath in sorted(tdir.glob("*.md")):
        profile = load_profile(fpath)
        if profile:
            profiles[profile.name] = profile
    return profiles


def run_child_task(
    profile: AgentProfile,
    task: str,
    provider: Any,  # BaseProvider
    tools: Any,     # ToolRegistry
    max_depth: int | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
) -> str:
    """Run a child agent task with tool loop.

    If session_id is provided, loads worker history from DB and persists
    turns after execution — enabling persistent worker memory across spawns.

    max_depth defaults to profile.max_depth, falling back to 10.
    """
    from .crow_state import CrowState
    from .paths import PROJECT_ROOT

    if max_depth is None:
        max_depth = getattr(profile, "max_depth", 10)

    # Restrict: strip spawn tools from child profiles
    allowed_tools = [t for t in profile.tools if t not in _RESTRICTED_CHILD_TOOLS]
    restricted = set(profile.tools) - set(allowed_tools)
    restriction_note = ""
    if restricted:
        restriction_note = f"\n\nRestricted tools (not available in child context): {', '.join(sorted(restricted))}"

    # Build system prompt: profile instructions + Crow rules + think-in-code
    system_parts = [profile.instructions]

    # Load Crow rules if available
    rules_path = PROJECT_ROOT / "RULES.md"
    if rules_path.exists():
        system_parts.append("\n## Crow Rules\n" + rules_path.read_text().strip())

    # Think-in-code + scratchpad conventions for workers
    system_parts.append("""
## Worker Conventions (MUST FOLLOW)
- Never read the scratchpad file directly. Use scripts (awk, grep, python) to query it.
- Write results to scratchpad using run_cmd: echo '...' >> scratchpath
- Append-only. Use ## STEP: | worker: | status: delimiters.
- If you need to know what other workers did, query: awk '/## STEP:.*status: done/,/## END/' scratchpad.md
- Trust your own memory: you have a persistent session. Past tasks are available.
""")

    system_parts.append(f"\n\nAvailable tools: {', '.join(allowed_tools)}{restriction_note}")
    system = "\n".join(system_parts)
    messages = [
        ChatMessage(role="system", content=system),
    ]

    # Load worker history if persistent session
    db = None
    if session_id:
        db = CrowState(db_path=db_path)
        db.create_session(session_id)
        history = db.history(session_id, limit=20)
        for turn in history:
            messages.append(ChatMessage(role=turn["role"], content=turn["content"]))

    messages.append(ChatMessage(role="user", content=task))

    # Build tool schemas — only profile-defined tools
    schemas = []
    profile_tool_set = set(profile.tools) if profile.tools else set(allowed_tools)
    for tool_name in allowed_tools:
        # Filter: only include tools listed in profile
        if profile.tools and tool_name not in profile_tool_set:
            continue
    for tool_name in allowed_tools:
        tool = tools.get(tool_name)
        if tool:
            schemas.append(tool.to_openai_tool())

    response = provider.chat(messages, tools=schemas or None)

    # Tool loop
    depth = 0
    while response.tool_calls and depth < max_depth:
        depth += 1
        messages.append(
            ChatMessage(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            )
        )

        for tc in response.tool_calls:
            name = tc.get("function", {}).get("name", "")
            if name in _RESTRICTED_CHILD_TOOLS:
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=f"Tool '{name}' is restricted in child agent context. Delegation requires the main agent session.",
                        tool_call_id=tc.get("id", ""),
                    )
                )
                continue
            execute_tool_call(tc, tools, messages)

        response = provider.chat(messages, tools=schemas or None)

    # Guard: empty response from provider — treat as error for crew retry
    if response and not response.content.strip():
        logger.warning("Worker returned empty response (finish=%s)", response.finish_reason)
        response = ChatResponse(
            content="Error: Worker produced no output — provider returned empty response.",
            tool_calls=[],
            finish_reason=response.finish_reason or "error",
            usage={"prompt_tokens": response.usage.get("prompt_tokens", 0),
                   "completion_tokens": response.usage.get("completion_tokens", 0)},
        )

    # Persist turn if worker has session
    if session_id and db:
        db.append_turn(session_id, "user", task)
        db.append_turn(session_id, "assistant", response.content)
        db.close()

    return response.content
