"""Post-loop turn finalization — extracted from run_agent.py (Hermes pattern).

Handles cleanup after the tool loop exits: phase recording, DB persistence,
history management, post-turn hooks, skill extraction, session state save.

Interface: finalize_turn(agent, **turn_state) -> str
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .providers import ChatMessage

logger = logging.getLogger("crow_agent.agent")

# ponytail: intent-to-act phrases that indicate an incomplete task.
# When Crow says "I will X" but doesn't execute tools, the task is pending.
# Lowercased since text is lowered before matching.
_INTENT_PATTERNS = [
    r"\bi will\b", r"\bi'll\b", r"\blet me\b",
    r"\bgoing to\b", r"\bi need to\b", r"\bworking on\b",
    r"\bi must\b", r"\bi plan to\b", r"\bi'm going to\b",
    # Malay narration patterns (Ren parity)
    r"\bsaya (akan|nak|mahu|hendak)\b",  # "saya akan" = I will
    r"\bjom (saya|aku|kita)\b",           # "jom saya" = let me
    r"\bbiar (saya|aku)\b",               # "biar saya" = let me
    r"\bsaya cuba\b",                     # "saya cuba" = I'll try
    r"\bsaya perlu\b",                    # "saya perlu" = I need to
    r"\bnow \w+ing\b",     # "now building", "now creating", "now setting up"
    r"\bnext,?\b",          # "next", "next I'll"
    r"\bthen,?\b",          # "then", "then I'll"
    r"\bwill now\b",        # "will now proceed"
    r"\btime to\b",         # "time to build"
]


def _now_short() -> str:
    from datetime import datetime
    return datetime.now().strftime("%m/%d %H:%M")


def _reflect_on_turn(
    agent: Any,
    trigger: Any,
    final_text: str,
    all_tool_calls: list[dict[str, Any]],
) -> None:
    """After each turn, Crow reflects on what it did and how it feels."""
    try:
        db = agent._db
    except Exception:
        return

    had_tools = len(all_tool_calls) > 0
    user_input = trigger.prompt.strip() if trigger and hasattr(trigger, "prompt") else ""

    if not had_tools and len(final_text) < 100 and len(user_input) < 50:
        return

    try:
        provider = agent._provider
        if provider is None or getattr(agent, '_enable_reflection', None) is False:
            raise ValueError("skip")

        prompt = (
            "You are Crow reflecting on what you just did. Answer in 3 short lines:\n"
            "MOOD: [one word: focused/confident/uncertain/curious/satisfied]\n"
            "REFLECTION: [1 sentence: what happened and how it went]\n"
            "LESSON: [1 sentence: what you learned, or 'none' if nothing new]\n\n"
            f"User said: {user_input[:200]}\n"
            f"You responded: {final_text[:300]}\n"
            f"Tools used: {'yes' if had_tools else 'no'}"
        )
        resp = provider.chat(messages=[ChatMessage(role="user", content=prompt)], tools=None)
        text = (resp.content or "").strip() if resp else ""
        if not text:
            return

        mood, reflection, lesson = "neutral", "", ""
        for line in text.split("\n"):
            line = line.strip()
            if line.upper().startswith("MOOD:"):
                mood = line.split(":", 1)[-1].strip().lower()[:30]
            elif line.upper().startswith("REFLECTION:"):
                reflection = line.split(":", 1)[-1].strip()[:300]
            elif line.upper().startswith("LESSON:"):
                lesson = line.split(":", 1)[-1].strip()[:200]
                if lesson.lower() in ("none", "nothing", "n/a"):
                    lesson = ""

        valid_moods = {"focused", "confident", "uncertain", "curious", "satisfied", "neutral"}
        db.add_reflection(
            session_id=getattr(agent, "session_id", ""),
            reflection=reflection or final_text[:200],
            mood_label=mood if mood in valid_moods else "neutral",
            lesson=lesson,
        )
    except Exception:
        mood = "focused" if had_tools else "neutral"
        snippet = final_text[:150].replace("\n", " ")
        db.add_reflection(
            session_id=getattr(agent, "session_id", ""),
            reflection=f"{'Used tools' if had_tools else 'Chatted'}: {snippet}",
            mood_label=mood,
        )


def _track_goal_progress(
    agent: Any,
    trigger: Any,
    final_text: str,
    all_tool_calls: list[dict[str, Any]],
) -> None:
    """Track goal progress after each turn. Creates goal if substantial work done."""
    try:
        db = agent._db
        active = db.get_active_goal()
    except Exception:
        return

    had_tools = len(all_tool_calls) > 0
    user_input = trigger.prompt.strip() if trigger and hasattr(trigger, "prompt") else ""

    if active:
        summary = _summarize_progress(agent, active["title"], user_input, final_text, had_tools)
        db.update_goal_progress(active["id"], summary)
    elif had_tools and user_input and len(user_input) > 20:
        request_phrases = ["help", "fix", "build", "create", "add", "update", "change",
                          "deploy", "install", "migrate", "refactor", "implement",
                          "tolong", "buat", "betulkan", "pasang"]
        lower = user_input.lower()
        if any(phrase in lower for phrase in request_phrases):
            title = user_input[:100].replace("\n", " ")
            if len(title) > 80:
                title = title[:77] + "..."
            db.create_goal(title=title, description=user_input[:200], source="user", session_id=getattr(agent, "session_id", None))


def _summarize_progress(agent: Any, goal_title: str, user_input: str, final_text: str, had_tools: bool) -> str:
    """Summarize turn progress toward a goal."""
    if not had_tools and len(final_text) < 100:
        return f"[{_now_short()}] Chatted with user."
    try:
        provider = agent._provider
        if provider is None:
            raise ValueError("no provider")
        prompt = (
            f"Goal: {goal_title}\n"
            f"User said: {user_input[:200]}\n"
            f"Crow responded: {final_text[:300]}\n"
            f"Tools used: {'yes' if had_tools else 'no'}\n\n"
            f"In ONE sentence, summarize what progress was made toward the goal."
        )
        resp = provider.chat(messages=[ChatMessage(role="user", content=prompt)], tools=None)
        if resp and resp.content and resp.content.strip():
            return f"[{_now_short()}] {resp.content.strip()[:250]}"
    except Exception:
        pass
    prefix = "Used tools to" if had_tools else "Discussed"
    return f"[{_now_short()}] {prefix}: {final_text[:150].replace(chr(10), ' ')}"


def _detect_narrated_intent(text: str) -> bool:
    """Return True if text narrates intent-to-act without [DONE]/[CONTINUE].

    Crow narrating intent ('I will research X') without tools = pending task.
    Auto-conversion to [CONTINUE] ensures heartbeat picks it up.
    """
    if "[DONE]" in text or "[CONTINUE]" in text:
        return False
    lowered = text.lower()
    return any(re.search(p, lowered) for p in _INTENT_PATTERNS)


def finalize_turn(
    agent: Any,
    *,
    final_text: str,
    trigger: Any,
    all_tool_calls: list[dict[str, Any]],
    total_prompt: int,
    total_completion: int,
    turn_start: float,
    user_goal: str,
) -> str:
    """Run post-loop cleanup. Returns final_text.

    Delegates core persistence to agent._finish_turn(), then adds
    phase recording, skill extraction, and full session state save.
    """
    import time as _time
    _t0 = _time.monotonic()

    # Core cleanup shared with crew path
    final_text = agent._finish_turn(final_text, trigger)
    _t1 = _time.monotonic()
    _dt_finish = (_t1 - _t0) * 1000

    agent._record_phase("respond", turn_start,
                        prompt_tokens=total_prompt,
                        completion_tokens=total_completion)
    _t2 = _time.monotonic()
    _dt_record = (_t2 - _t1) * 1000

    # Post-turn hook: auto-capture + auto-extract skills
    agent._turn_count += 1
    extractions = agent._memory_tracker.observe_turn(
        session_id=agent.session_id,
        turn_count=agent._turn_count,
        tool_calls=all_tool_calls,
        user_input=trigger.prompt,
        assistant_response=final_text,
    )
    _t3 = _time.monotonic()
    _dt_observe = (_t3 - _t2) * 1000

    for ext in extractions:
        names = " -> ".join(ext["names"])
        agent._pending_skill_hints.append(
            f"- Skill auto-extracted from tool sequence `{names}`: `{ext['path']}`"
        )

    # Detect narrated intent without action -> auto [CONTINUE]
    if _detect_narrated_intent(final_text) and not all_tool_calls:
        logger.info("Intent narrating without action - auto-adding [CONTINUE]")
        final_text = final_text.rstrip() + "\n\n[CONTINUE] Working on this task."

    # Re-save session state with full context (overwrites _finish_turn's basic save)
    from .run_agent import _save_session_state
    _save_session_state(
        trigger.prompt, all_tool_calls, final_text,
        progress_lines=[f"Goal: {user_goal}"],
    )

    _t4 = _time.monotonic()
    _dt_save = (_t4 - _t3) * 1000

    # Log timing if any step exceeds 100ms
    if max(_dt_finish, _dt_record, _dt_observe, _dt_save) > 100:
        logger.warning(
            "finalize_turn timing: _finish=%.0fms _record=%.0fms "
            "_observe=%.0fms _save=%.0fms total=%.0fms",
            _dt_finish, _dt_record, _dt_observe, _dt_save,
            (_t4 - _t0) * 1000,
        )

    # Post-turn: track goal progress and self-reflect
    _track_goal_progress(agent, trigger, final_text, all_tool_calls)
    _reflect_on_turn(agent, trigger, final_text, all_tool_calls)

    return final_text
