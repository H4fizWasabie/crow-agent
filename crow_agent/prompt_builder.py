"""Tiered prompt assembly with cache-optimized layering and budget management.

Tier 1 (Stable):    identity, tool definitions — cached by provider.
Tier 2 (Context):   SOUL.md, USER.md, MEMORY.md — read at startup, injected each turn.
Tier 3 (Volatile):  timestamps, FTS recall results, short-term history — turn-scoped.

Budget: history turns are truncated to fit within the model's context window,
newest-first. Excess turns get a summary marker.

Architecture (Hermes-inspired): Tier 1 blocks are modular constants. Each togglable —
identity swapped per agent profile, per-tool guidance injected only when that tool is loaded.
Rules now in RULES.md (injected every turn, not hardcoded here).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("crow_agent.prompts")

# Default token budget — 35K matches VPS production data (clean turns: 10K-28K).
DEFAULT_CONTEXT_BUDGET = 120_000
# Tokens reserved for the model's response (never given to history)
RESPONSE_RESERVE = 4_096
# Minimum full turns to keep (newest), even when over budget
MIN_HISTORY_TURNS = 3


def resolve_context_budget(provider: Any) -> int:
    """Resolve context budget from model window, falling back to env/default."""
    env_budget = os.environ.get("CROW_CONTEXT_BUDGET", "")
    if env_budget:
        return int(env_budget)
    try:
        window = getattr(getattr(provider, "config", None), "context_window", 0)
        if window:
            return window - RESPONSE_RESERVE
    except Exception:
        pass
    return DEFAULT_CONTEXT_BUDGET

import functools

_encoding_cache: dict[str, Any] = {}  # model_name_or_encoding → encoding


# Model → tiktoken encoding mapping.
KNOWN_TOKENIZERS: dict[str, str] = {
    "gpt-4o": "o200k_base",
    "gpt-4": "cl100k_base",
    "gpt-3.5": "cl100k_base",
    "o1": "o200k_base",
    "o3": "o200k_base",
    "deepseek": "o200k_base",
    "gemini": "o200k_base",
    "claude": "",
}


@functools.lru_cache(maxsize=32)
def _get_tokenizer(model: str | None = None) -> Any:
    """Get tiktoken encoding for the given model. Falls back to o200k_base."""
    try:
        import tiktoken

        enc_name = None
        if model:
            for prefix, enc in KNOWN_TOKENIZERS.items():
                if model.lower().startswith(prefix):
                    enc_name = enc or None
                    break
        if not enc_name:
            enc_name = "o200k_base"

        if enc_name:
            try:
                return tiktoken.get_encoding(enc_name)
            except Exception:
                return tiktoken.get_encoding("cl100k_base")

        return None
    except ImportError:
        logger.warning("tiktoken not installed; falling back to len() approximation")
        return None


def count_tokens(text: str, model: str | None = None) -> int:
    """Count tokens using model-aware tokenizer (or len//4 fallback)."""
    enc = _get_tokenizer(model)
    if enc:
        return len(enc.encode(text, disallowed_special=()))
    if model and model.lower().startswith("claude"):
        return len(text) // 3 + 1
    return len(text) // 4


def truncate_history_by_budget(
    history: list[dict[str, Any]],
    system_tokens: int,
    user_input: str,
    context_budget: int = DEFAULT_CONTEXT_BUDGET,
    reserve: int = RESPONSE_RESERVE,
    min_turns: int = MIN_HISTORY_TURNS,
    model: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Trim old history turns to stay within context budget.

    Pair-aware: when an assistant with tool_calls is dropped, its tool
    result messages are also dropped to prevent orphaned tool messages.
    """
    if not history:
        return history, []

    available = context_budget - reserve - system_tokens
    available -= count_tokens(user_input, model=model)

    kept_full: list[dict[str, Any]] = []
    dropped_turns: list[dict[str, Any]] = []
    running = 0
    drop_tool_results = False

    for turn in reversed(history):
        t = count_tokens(turn.get("content", ""), model=model) + 4

        # Pair-aware: drop orphaned tool results (Ren parity)
        if drop_tool_results and turn.get("role") == "tool":
            dropped_turns.append(turn)
            continue
        drop_tool_results = False

        if running + t <= available or len(kept_full) < min_turns:
            kept_full.append(turn)
            running += t
        else:
            dropped_turns.append(turn)
            if turn.get("role") == "assistant" and turn.get("tool_calls"):
                drop_tool_results = True

    kept_full.reverse()
    dropped_turns.reverse()

    # Remove orphaned tools from kept (boundary pairs)
    kept_full = _remove_orphaned_tools(kept_full)

    if dropped_turns:
        kept_full.insert(0, {
            "role": "system",
            "content": f"[{len(dropped_turns)} earlier turns summarized due to context budget]",
        })

    return kept_full, dropped_turns


def _remove_orphaned_tools(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove tool messages that don't have a preceding assistant with tool_calls."""
    result = []
    has_assistant_with_calls = False
    for turn in history:
        if turn.get("role") == "assistant" and turn.get("tool_calls"):
            has_assistant_with_calls = True
        elif turn.get("role") == "tool" and not has_assistant_with_calls:
            continue
        elif turn.get("role") != "tool":
            has_assistant_with_calls = False
        result.append(turn)
    return result


# ── Tiered prompt assembly ──

MAX_MEMORY_LINES = 100


def load_context_file(path: str | Path, fallback: str = "", max_lines: int = 0) -> str:
    """Read a context markdown file. Returns fallback if missing."""
    try:
        content = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return fallback

    if max_lines:
        lines = content.split("\n")
        if len(lines) > max_lines:
            archived = len(lines) - max_lines
            content = "\n".join(lines[-max_lines:])
            content += f"\n\n*[{archived} older entries archived]*"
    return content


# ── Modular Tier 1 Constants (Hermes-inspired, Crow-compressed) ──────────
# Behavioral rules now live in RULES.md (injected every turn).
# Constants kept minimal — only what RULES.md doesn't cover.

TOOL_USE_ENFORCEMENT_MODELS = ("gpt", "codex", "gemini", "gemma", "grok", "deepseek", "qwen", "glm")


def build_tier1(
    identity: str,
    tool_schemas: list[dict[str, Any]],
    *,
    loaded_tools: set[str] | None = None,
) -> list[dict[str, str]]:
    """Tier 1: stable instructions + tool list."""
    tool_names = [
        t["function"]["name"]
        for t in tool_schemas
        if t.get("type") == "function"
    ]
    loaded_tools = loaded_tools or set()

    parts: list[str] = [identity]

    # Per-tool guidance — only when tool is loaded
    if "memory_store" in loaded_tools or "memory_search" in loaded_tools:
        parts.append(
            "## Memory\n"
            "memory_store: save durable facts, prefs, patterns.\n"
            "memory_search: check before asking user for past context."
        )

    parts.append(f"## Tools\n{', '.join(tool_names)}")

    blocks = [
        {"type": "text", "text": "\n\n".join(parts)},
        {
            "type": "text",
            "text": f"## Tool Schemas\n```json\n{_dumps(tool_schemas)}\n```",
        },
    ]
    return blocks


def build_tier2(
    soul: str = "",
    user_md: str = "",
    memory: str = "",
) -> list[dict[str, str]]:
    """Tier 2: context files. Stable across a session, set at startup."""
    blocks: list[dict[str, str]] = []
    if soul:
        blocks.append({"type": "text", "text": f"## SOUL.md\n{soul}"})
    if user_md:
        blocks.append({"type": "text", "text": f"## USER.md\n{user_md}"})
    if memory:
        blocks.append({"type": "text", "text": f"## MEMORY.md\n{memory}"})
    return blocks


def build_tier3(
    history: list[dict[str, Any]],
    fts_results: list[dict[str, Any]] | None = None,
    timestamp: str | None = None,
) -> list[dict[str, str]]:
    """Tier 3: volatile per-turn state."""
    blocks: list[dict[str, str]] = []

    ts = timestamp or time.strftime("%Y-%m-%d %H:%M:%S %Z")
    blocks.append({"type": "text", "text": f"## Current Time\n{ts}"})

    if fts_results:
        recall_text = "## Recall From Past Sessions\n"
        for r in fts_results:
            recall_text += f"[{r.get('session_id','?')}] {r.get('role','?')}: {r.get('content','')[:300]}\n"
        blocks.append({"type": "text", "text": recall_text})

    if history:
        hist_text = "## Conversation History (recent)\n"
        for turn in history:
            hist_text += f"**{turn['role']}**: {turn['content'][:500]}\n"
        blocks.append({"type": "text", "text": hist_text})

    return blocks


def build_system_message(
    identity: str,
    tool_schemas: list[dict[str, Any]],
    soul: str = "",
    user_md: str = "",
    memory: str = "",
    *,
    loaded_tools: set[str] | None = None,
) -> list[dict[str, str]]:
    """Assemble the full system message from Tiers 1 + 2."""
    content: list[dict[str, str]] = []
    content.extend(build_tier1(identity, tool_schemas, loaded_tools=loaded_tools))
    content.extend(build_tier2(soul, user_md, memory))
    return content


def build_user_turn_message(
    user_input: str,
    history: list[dict[str, Any]],
    fts_results: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Assemble a user turn message with Tier 3 volatile context prepended."""
    blocks = build_tier3(history=history, fts_results=fts_results)
    blocks.append({"type": "text", "text": f"## Current Request\n{user_input}"})
    return blocks


def _dumps(obj: Any) -> str:
    return json.dumps(obj, indent=None, separators=(",", ":"), ensure_ascii=False)
