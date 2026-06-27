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

    return final_text
