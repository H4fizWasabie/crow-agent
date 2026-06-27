"""Post-turn memory observation tracker.

Records user preferences from conversation signals to USER.md.
No LLM calls. Keeps a small state file to avoid re-observing the same events.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import PROJECT_ROOT

# User preference signal detection — high-precision keywords only
_PREF_PATTERN = re.compile(r"\b(prefer|prefers)\b", re.IGNORECASE)
# Negative context — skip conversational false positives
_NEGATIVE_CONTEXT = re.compile(
    r"\b(don'?t (know|think|worry|understand|remember|see|care)|"
    r"do(n'?t)? (not )?(know|think|worry|understand))",
    re.IGNORECASE,
)

# Inline LLM extraction config
# ponytail: stretched from 5 to 10 — halves pref extraction LLM calls
_INLINE_EXTRACT_INTERVAL = 10

logger = logging.getLogger("crow_agent.memory_tracker")

DEFAULT_STATE_PATH = Path.home() / ".crow_agent" / "memory_state.json"
DEFAULT_SKILLS_DIR = PROJECT_ROOT / "skills"
TURN_THRESHOLDS = [10, 50, 100, 500]
SEQUENCE_MIN_LENGTH = 5
# AUTO_SECTION_RE removed — vault is canonical memory, no MEMORY.md auto-sections


class SequenceExtractor:
    """Evaluates tool sequences via OpenRouter LLM. Extracts meaningful workflows as skills."""

    _SEQUENCE_EXTRACT_PROMPT = """You are analyzing a tool call sequence from an AI assistant (Crow). Determine if this sequence represents a meaningful, reusable workflow worth saving as a skill.

Tool sequence: {names}

Criteria for meaningful workflow:
- Has a clear intent or goal (research, fix, deploy, investigate)
- Multiple tools working together toward that goal
- Would be useful to repeat in future sessions

Not meaningful:
- Too short or trivial
- Random combination without clear intent
- Error recovery or one-off debugging

Reply ONLY with a JSON object. No markdown, no explanation.

Examples:
{{"is_workflow": false}}
{{"is_workflow": true, "name": "research-topic", "description": "Research a topic by searching and summarizing", "steps": ["Search the web for information", "Read relevant pages", "Synthesize findings into a summary"]}}"""

    def __init__(self, state: dict) -> None:
        self._state = state
        self._skills_dir = DEFAULT_SKILLS_DIR

    def note(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Evaluate tool sequence via LLM gate. Returns extracted skills list.

        ponytail: LLM evaluation runs in a daemon thread so it never
        blocks the event loop. Results are written to disk asynchronously;
        this means extracted skills won't appear until a later turn.
        """
        names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
        if len(names) < SEQUENCE_MIN_LENGTH:
            return []

        # Fire-and-forget: evaluate in background, write skill if worthwhile
        import threading
        t = threading.Thread(
            target=self._evaluate_and_write,
            args=(names,),
            daemon=True,
        )
        t.start()
        return []  # never blocks — results arrive on a future turn

    def _evaluate_and_write(self, names: list[str]) -> None:
        """Evaluate tool sequence via LLM, write skill if worthwhile.

        Called from daemon thread — never blocks the event loop.
        """
        try:
            provider, ChatMessage = _resolve_cheap_provider()
        except (FileNotFoundError, ValueError) as e:
            logger.debug("Sequence evaluation skipped: %s", e)
            return

        prompt = self._SEQUENCE_EXTRACT_PROMPT.format(names=" → ".join(names))
        try:
            response = provider.chat(
                [ChatMessage(role="user", content=prompt)],
                max_tokens=1000,
            )
            raw = response.content.strip()
            m = re.search(r"```(?:json)?\s*\n?(.*?)```", raw, re.DOTALL)
            if m:
                raw = m.group(1).strip()
            result = json.loads(raw)
        except Exception as e:
            logger.debug("Sequence evaluation failed: %s", e)
            return

        if not result.get("is_workflow"):
            return

        skill_path = self._write_skill(result, names)
        if skill_path:
            logger.info("Extracted skill: %s", result["name"])

    def _write_skill(self, result: dict, names: list[str]) -> str | None:
        """Write a skill file from LLM evaluation result."""
        name = result.get("name", "").strip().lower().replace(" ", "-")
        if not name:
            return None
        # Skip mechanical names from old threshold system — LLM must produce descriptive names
        if name.startswith("auto-"):
            logger.debug("Skipped mechanical skill name: %s", name)
            return None

        self._skills_dir.mkdir(parents=True, exist_ok=True)
        fpath = self._skills_dir / f"{name}.md"
        if fpath.exists():
            return str(fpath)  # Already extracted, no overwrite

        description = result.get("description", f"Auto-extracted workflow: {' → '.join(names)}")
        steps = result.get("steps", [])
        steps_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))

        content = f"""---
name: {name}
description: {description}
intent: automation
triggers:
  - {names[0] if names else name}
---

# {name}

{description}

## Steps

{steps_text}
"""
        try:
            fpath.write_text(content.strip() + "\n", encoding="utf-8")
            logger.info("Wrote skill file: %s", fpath)
            return str(fpath)
        except OSError as exc:
            logger.warning("Failed to write skill %s: %s", fpath, exc)
            return None




class MemoryTracker:
    """Lightweight post-turn observer. Delegates to specialized sub-trackers."""

    def __init__(
        self,
        memory_path: str | Path = "MEMORY.md",
        state_path: str | Path | None = None,
    ) -> None:
        from crow_agent.paths import PROJECT_ROOT
        mem = Path(memory_path)
        self._memory_path = mem if mem.is_absolute() else PROJECT_ROOT / mem
        self._state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self._state: dict[str, Any] = self._load_state()
        self._skill_buffer: list[tuple[str, str]] = []
        self._dirty = False

        # Inline extraction state
        self._user_msg_buffer: list[str] = []
        self._inline_turn_counter = 0

        # Delegated concerns
        self._sequence_extractor = SequenceExtractor(self._state)

    # --- public ---

    def observe_turn(
        self,
        session_id: str,
        turn_count: int,
        tool_calls: list[dict[str, Any]],
        user_input: str = "",
        assistant_response: str = "",
    ) -> list[dict[str, Any]]:
        """Called after each turn. Returns newly auto-extracted skills."""
        extractions = self._sequence_extractor.note(tool_calls)

        # Turn milestone self-evaluation (Phase 6 — Ren parity)
        if turn_count in TURN_THRESHOLDS:
            import threading
            t = threading.Thread(
                target=self._evaluate_milestone,
                args=(turn_count,),
                daemon=True,
            )
            t.start()

        self._state["last_session_id"] = session_id
        self._dirty = True

        self._save_state()
        return extractions

    def note_matched_skills(self, skills: list[tuple[str, str]]) -> None:
        """Record matched skills for context assembly."""
        self._skill_buffer = skills
        self._dirty = True

    def inject_learnings(self) -> str | None:
        """Return a summary of recent learnings for context injection."""
        if not self._skill_buffer:
            return None
        lines = ["## Recent Learnings"]
        for name, source in self._skill_buffer[:5]:
            lines.append(f"- {name} ({source})")
        return "\n".join(lines)

    def _load_state(self) -> dict[str, Any]:
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_state(self) -> None:
        if not self._dirty:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        self._dirty = False

    def _append_user_model(self, update: str) -> None:
        """Append an observation to USER_MODEL.md. (Phase 6)"""
        if not update or not update.strip():
            return
        vault_path = Path.home() / ".crow_agent" / "USER_MODEL.md"
        vault_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        try:
            with open(vault_path, "a", encoding="utf-8") as f:
                f.write(f"\n## {ts}\n{update}\n")
        except OSError:
            pass

    def _evaluate_milestone(self, turn_count: int) -> None:
        """LLM self-evaluation at turn milestones (10, 50, 100, 500). (Phase 6)

        Fire-and-forget via daemon thread — never blocks the event loop.
        """
        try:
            provider, ChatMessage = _resolve_cheap_provider()
        except (FileNotFoundError, ValueError) as e:
            logger.debug("Milestone evaluation skipped: %s", e)
            return

        prompt = (
            f"You are Crow, an autonomous AI agent. You have completed {turn_count} turns "
            "of conversation with your creator. Take a moment to reflect:\n\n"
            "1. What patterns do you notice in the user's requests and preferences?\n"
            "2. What could you do better? Any recurring mistakes or missed opportunities?\n"
            "3. What one skill or capability would make you more helpful?\n\n"
            "Respond in 3-5 concise bullet points. Be honest and specific."
        )
        try:
            response = provider.chat(
                [ChatMessage(role="user", content=prompt)],
                max_tokens=500,
            )
            text = response.content.strip()
        except Exception as e:
            logger.debug("Milestone evaluation LLM call failed: %s", e)
            return

        if text:
            self._append_user_model(f"[Turn {turn_count} self-evaluation]\n{text}")
            logger.info("Turn %d milestone evaluation written", turn_count)

# ── module-level helpers ──

# prune_memory removed — vault is the canonical memory store, no auto-captured sections to prune


def _resolve_cheap_provider():
    """Load a cheap provider for background tasks from ~/.crow_agent/providers.json.

    Tries opencode-zen, then openrouter-auto, then openrouter.
    """
    providers_path = Path.home() / ".crow_agent" / "providers.json"
    if not providers_path.exists():
        raise FileNotFoundError(
            f"No providers.json at {providers_path}. Crow agent not configured."
        )
    data = json.loads(providers_path.read_text(encoding="utf-8"))
    providers = data.get("providers", {})
    # Prefer opencode-zen (free tier), fallback to openrouter variants
    cfg = providers.get("opencode-zen") or providers.get("openrouter-auto") or providers.get("openrouter")
    if not cfg:
        raise ValueError("no cheap provider found in providers.json (tried opencode-zen, openrouter-auto, openrouter)")

    from crow_agent.providers import ChatMessage, resolve_provider

    provider = resolve_provider(
        "opencode-zen" if "opencode-zen" in providers else ("openrouter-auto" if "openrouter-auto" in providers else "openrouter"),
        api_key=cfg.get("api_key"),
        base_url=cfg.get("base_url"),
        model=cfg.get("model"),
        provider_manager=None,
    )
    return provider, ChatMessage
