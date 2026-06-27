"""Context assembler — RECALL + ASSEMBLE phases of the agent turn.

Extracted from run_agent.py. Takes all dependencies explicitly via parameters
rather than relying on self._* attributes — the interface IS the dependency list.

Interface: assemble_context(user_input, db, provider, ...) → messages
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from .crow_state import CrowState
from .memory_tracker import MemoryTracker
from .providers import BaseProvider, ChatMessage
from .prompt_builder import (
    build_system_message,
    build_user_turn_message,
    count_tokens,
    resolve_context_budget,
    truncate_history_by_budget,
)
from .run_agent import _load_session_state, _save_session_state, TriggerSource  # soulmates
from .self_model import SelfModel
from .skills_system import SkillsIndex

logger = logging.getLogger("crow_agent.agent")


def assemble_context(
    user_input: str,
    *,
    db: CrowState,
    provider: BaseProvider,
    history: list[dict[str, Any]],
    memory_tracker: MemoryTracker,
    skills: SkillsIndex,
    memory: str,
    soul: str,
    user_md: str,
    identity: str,
    fts_limit: int = 5,
    history_limit: int = 20,
    pending_skill_hints: list[str] | None = None,
    shown_reports: set[str] | None = None,
    trigger_source: TriggerSource = TriggerSource.USER,
    provider_manager: Any | None = None,
    cached_system_content: list[dict[str, str]] | None = None,
    self_model: SelfModel | None = None,
    foreman: Any | None = None,
) -> tuple[list[ChatMessage], list[str], set[str]]:
    """Build messages for the CALL phase from user input + stored context.

    Returns (messages, new_pending_hints, shown_reports).
    Messages are ready for provider.chat().
    """
    from .paths import PROJECT_ROOT

    context_injections: list[str] = []

    # ── RECALL ──
    # Semantic-first: run context sources (includes semantic search) first.
    # If semantic finds results, skip FTS5 to avoid redundant keyword results.
    from .context_sources import register_default_sources, collect_context
    _sources = register_default_sources(skills, memory_tracker, str(PROJECT_ROOT))
    _t1, _t2, _t3 = collect_context(_sources, user_input, db)
    context_injections.extend(_t1)
    context_injections.extend(_t2)
    context_injections.extend(_t3)

    # Check if semantic search returned strong matches (skip FTS5 if so)
    _semantic_found = any("## Semantic Matches" in inj for inj in _t1 + _t2 + _t3)
    if _semantic_found:
        fts_results = []  # skip FTS5 — semantic already covered it
    else:
        fts_results = db.search(user_input, limit=fts_limit)

    # ── RECALL (continued) ──

    # Initiative turns: inject recent autonomous session ticks as context
    if trigger_source == TriggerSource.HEARTBEAT:
        try:
            _auto_ticks = db.history("__autonomous__", limit=5)
            if _auto_ticks:
                _tick_lines = []
                for t in _auto_ticks[-3:]:
                    _c = t.get("content", "")[:200]
                    _tick_lines.append(f"- {_c}")
                _awareness = "## Situational Awareness (recent heartbeat ticks)\n" + "\n".join(_tick_lines)
                context_injections.append(_awareness)
        except Exception:
            pass  # ponytail: best-effort, never block autonomous turn

    # ── ASSEMBLE ──
    # Reload memory (re-read context files so mid-session learnings visible)
    # memory already loaded by _reload_memory() in _prepare_turn — passed as parameter

    # ponytail: reuse cached system content when provided
    if cached_system_content is not None:
        system_content = cached_system_content
    else:
        system_content = build_system_message(
            identity=identity,
            tool_schemas=[],
            soul=soul,
            user_md=user_md,
            memory=memory,
        )

    # Think in Code injection (Ren parity) — bash-first directive
    system_content.append({
        "type": "text",
        "text": (
            "## Think in Code\n"
            "Use run_cmd for ALL multi-step data tasks. One bash command replaces "
            "10+ individual read/grep/find calls. Write a Python script and run it "
            "instead of many sequential tool calls. Think first, then execute in one shot."
        ),
    })

    # Budget-aware history truncation
    system_tokens = count_tokens(
        _flatten(system_content),
        model=provider.config.model,
    )
    trimmed_history, dropped_turns = truncate_history_by_budget(
        history,
        system_tokens=system_tokens,
        user_input=user_input,
        model=provider.config.model,
    )
    if dropped_turns:
        dropped_summary = _summarize_turns(dropped_turns, provider, provider_manager)
        if dropped_summary:
            trimmed_history.insert(0, {"role": "system", "content": dropped_summary})

    user_content = build_user_turn_message(
        user_input=user_input,
        history=trimmed_history,
        fts_results=fts_results,
    )
    # -- Tiered context injection (Move 2) --
    _budget = resolve_context_budget(provider)
    _model = provider.config.model
    _used = count_tokens(_flatten(system_content), model=_model)
    _used += count_tokens(_flatten(user_content), model=_model)
    # Source-level budget logging (Phase 4)
    _budget_log: dict[str, int] = {
        "system": count_tokens(_flatten(system_content), model=_model),
        "user": count_tokens(_flatten(user_content), model=_model),
    }
    _tier1_inj: list[str] = []
    _tier2_inj: list[str] = []
    _tier3_inj: list[str] = []
    for inj in context_injections:
        if "## Matched Skills" in inj or "## Skill Extraction Hints" in inj or "## RULES" in inj:
            _tier1_inj.append(inj)
        elif "## Semantic Matches" in inj or "Recent learnings" in inj or "resume" in inj.lower() or "Recall" in inj:
            _tier2_inj.append(inj)
        else:
            _tier3_inj.append(inj)
    for inj in _tier1_inj:
        cost = count_tokens(inj, model=_model)
        _used += cost
        _budget_log["tier1"] = _budget_log.get("tier1", 0) + cost
        if _used > _budget:
            raise RuntimeError(f"Tier 1 exceeds budget ({_used}>{_budget})")
        user_content.append({"type": "text", "text": inj})
    _tier2_limit = int(_budget * 0.8)
    for inj in _tier2_inj:
        _tokens = count_tokens(inj, model=_model)
        if _used + _tokens <= _tier2_limit:
            _used += _tokens
            _budget_log["tier2"] = _budget_log.get("tier2", 0) + _tokens
            user_content.append({"type": "text", "text": inj})
        else:
            _budget_log["tier2_overflow"] = _budget_log.get("tier2_overflow", 0) + _tokens
    for inj in _tier3_inj:
        _tokens = count_tokens(inj, model=_model)
        if _used + _tokens <= _budget:
            _used += _tokens
            _budget_log["tier3"] = _budget_log.get("tier3", 0) + _tokens
            user_content.append({"type": "text", "text": inj})
        elif _used < _budget:
            _remaining = (_budget - _used) * 4
            if _remaining > 200:
                _summary = inj[:_remaining] + "..."
                _used += count_tokens(_summary, model=_model)
                _budget_log["tier3_truncated"] = _budget_log.get("tier3_truncated", 0) + _tokens
                user_content.append({"type": "text", "text": _summary})
            else:
                _budget_log["tier3_overflow"] = _budget_log.get("tier3_overflow", 0) + _tokens
        else:
            _budget_log["tier3_overflow"] = _budget_log.get("tier3_overflow", 0) + _tokens

    # Log per-source budget breakdown (Phase 4)
    total = sum(_budget_log.values())
    if total > 1000:
        details = ", ".join(f"{k}={v}" for k, v in sorted(_budget_log.items()) if v > 0)
        logger.info("Context budget: %d tokens — %s", total, details)

    # Build message list
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=_flatten(system_content))
    ]
    for turn in trimmed_history:
        messages.append(ChatMessage(role=turn["role"], content=turn["content"]))
    messages.append(ChatMessage(role="user", content=_flatten(user_content)))

    # Inject self-awareness status card
    if self_model is not None:
        status = self_model.to_prompt_chunk()
    else:
        status = _build_self_status(db, provider, history)
    messages.insert(1, ChatMessage(role="system", content=status))

    # Inject foreman crew updates (Phase 9)
    if foreman is not None:
        foreman_text = foreman.context_text()
        if foreman_text:
            messages.insert(2, ChatMessage(role="system", content=foreman_text))

    return messages, pending_skill_hints or [], shown_reports or set()


def _flatten(blocks: list[dict[str, str]]) -> str:
    """Flatten content blocks to a single string."""
    parts = [b["text"] for b in blocks if b.get("type") == "text"]
    return "\n\n".join(parts)


def _build_self_status(
    db: CrowState,
    provider: BaseProvider,
    history: list[dict[str, Any]],
) -> str:
    """Build self-awareness status card for system prompt."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    provider_name = getattr(provider, "config", None)
    provider_str = f"{provider_name.name} ({provider_name.model})" if provider_name else "unknown"

    lines = [
        "## My State",
        f"Time: {now}",
        f"Provider: {provider_str}",
        "Capabilities: semantic-recall, budget-tiers, crew-failover, initiative",
            ]
    if history:
        lines.append(f"Context turns: {len(history)}")

    return "\n".join(lines)


def _summarize_turns(
    turns: list[dict[str, Any]],
    provider: BaseProvider,
    provider_manager: Any | None = None,
) -> str:
    """Generate condensed summary of dropped conversation turns.

    Uses verifier provider (if available via provider_manager) to isolate
    summarization budget from the main agent provider.
    """
    if not turns:
        return ""

    # Resolve verifier provider for budget isolation
    summarizer = provider
    if provider_manager is not None:
        try:
            from .agent_profiles import load_all_profiles
            profs = load_all_profiles()
            verifier_profile = profs.get("verifier")
            verifier_name = getattr(verifier_profile, "provider_name", None) if verifier_profile else None
            if verifier_name:
                from .providers import resolve_provider
                summarizer = resolve_provider(verifier_name, provider_manager=provider_manager)
        except Exception:
            pass  # ponytail: fall back to main provider

    lines = []
    for t in turns:
        content = t.get("content", "")
        short = content[:300].replace("\n", " ")
        if len(content) > 300:
            short += "..."
        lines.append(f"[{t.get('role', '?')}]: {short}")

    prompt = (
        "Summarize the following conversation turns in 2-4 sentences. "
        "Focus on key decisions, user preferences, and facts learned. "
        "Omit greetings and small talk.\n\n"
    )
    try:
        resp = summarizer.chat(
            messages=[
                ChatMessage(role="system", content=prompt),
                ChatMessage(role="user", content="\n".join(lines)),
            ],
            max_tokens=500,
        )
        summary = resp.content.strip()
        if summary:
            return f"Summary of earlier turns:\n{summary}"
    except Exception:
        pass

    return ""
