"""AIAgent — the core multi-turn orchestrator.

State machine per turn:
    IDLE → RECALL → ASSEMBLE → CALL → [TOOL_LOOP] → RESPOND → IDLE

    RECALL:          FTS5 search on history for context relevant to user input.
    ASSEMBLE:        Build tiered system + user messages (stable → context → volatile).
    CALL:            Send to LLM provider, get response (possibly with tool_calls).
    TOOL_LOOP:       Execute each tool call, append results, call provider again.
                     Repeat until response has no tool_calls.
    RESPOND:         Final assistant text returned to caller. Turn recorded in DB.

    On any step failure → ERROR → IDLE (error propagated to caller).

    ADR v2: quality gates removed. Discipline via skills/control.md.
    Discipline now comes from skills/control.md and skills/ponytail.md —
    skills the LLM follows voluntarily, not code gates that fight it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, AsyncGenerator

from .crow_state import CrowState
from .heartbeat_engine import mark_user_active, mark_user_inactive
from .memory_tracker import MemoryTracker
from .self_model import SelfModel
from .crew import decompose_task, execute_plan, merge_results, CrewScratchpad
from .turn_finalizer import finalize_turn, _detect_narrated_intent
from .error_tracker import get_error_tracker
from pathlib import Path as _Path

# ── Checkpoint: crash recovery for mid-task interruption ──
_CHECKPOINT_DIR = _Path.home() / ".crow_agent" / "active_tasks"

def _save_checkpoint(session_id: str, goal: str, round_num: int,
                     tool_names: list[str], last_output: str) -> None:
    """Save a checkpoint for crash recovery."""
    import json, time
    _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    snippets = [t for t in tool_names[-3:]] if tool_names else []
    discovery = last_output.strip()[:200] if (last_output.strip() 
                and not last_output.startswith("Processed ")) else ""
    cp = _CHECKPOINT_DIR / f"{session_id}.json"
    existing = {}
    if cp.exists():
        try:
            existing = json.loads(cp.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    existing.update({
        "session_id": session_id,
        "goal": goal[:500],
        "round": round_num,
        "discoveries": existing.get("discoveries", []) + ([discovery] if discovery else []),
        "tools_used": list(dict.fromkeys(existing.get("tools_used", []) + tool_names)),
        "last_action": discovery or f"Completed round {round_num}",
        "updated": time.time(),
        "status": existing.get("status", "active"),
        "retry_count": existing.get("retry_count", 0),
    })
    cp.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    logger.info("Checkpoint saved: %s round %d", session_id, round_num)

def _load_checkpoint(session_id: str) -> dict | None:
    """Load checkpoint if one exists for this session."""
    import json
    cp = _CHECKPOINT_DIR / f"{session_id}.json"
    if cp.exists():
        try:
            return json.loads(cp.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None

def _clear_checkpoint(session_id: str) -> None:
    """Delete checkpoint on successful completion."""
    cp = _CHECKPOINT_DIR / f"{session_id}.json"
    if cp.exists():
        cp.unlink()
        logger.info("Checkpoint cleared: %s", session_id)

# Tool failure messages (shared between sync and async tool loops)
_FAILURE_ABORT_MSG = (
    "⚠️ Stopped — 3 consecutive tool calls failed. "
    "Last error: {}\n\n"
    "Could you help me understand what you need? "
    "I'll try a different approach."
)

# Tool timeouts
_TOOL_TIMEOUT = 30  # seconds per individual tool call

# Tool loop reminder — injected before each LLM call in the tool loop
# and as interceptor when LLM responds with text instead of tools.
# Internal monologue (c83a302): text without tools is preserved as context.
_LOOP_EXECUTE_REMINDER = (
    "[SYSTEM] Your text is preserved as context — continue working. "
    "Call a tool NOW to act on it. "
    "If you send text without tools again, the turn ends."
)
# Post-tool nudge: LLM wrote status update instead of calling next tool.
# Escalates: first a firm reminder, second a hard demand.
_POST_TOOL_NUDGE_1 = (
    "[SYSTEM] DO NOT narrate progress. Your text is saved as context. "
    "Call the NEXT tool NOW. Do not describe what you will do — do it."
)
_POST_TOOL_NUDGE_2 = (
    "[SYSTEM] FINAL WARNING. You are narrating instead of working. "
    "Your next response MUST contain a tool call. "
    "Just use a tool directly. No markers needed."
)
# Budget exhaustion prompt injected during tool loop when round limit is hit
_BUDGET_EXHAUSTION_PROMPT = (
    "[SYSTEM] Round limit reached. "
    "Deliver what you've actually completed — data, files, code, or URLs. "
    "Do not list what remains undone."
)

# Shared thread pool for tool execution — avoids creating + destroying
# a ThreadPoolExecutor per call. 4 workers is plenty since agent processes
# one turn at a time (tool calls are sequential per turn).
# Created lazily on first use; survives for the process lifetime.
_TOOL_EXECUTOR: "concurrent.futures.ThreadPoolExecutor | None" = None
_TOOL_EXECUTOR_LOCK = threading.Lock()

# Dangerous command patterns — blocked silently with helpful error
# Narrow blocklist: only patterns with ZERO legitimate use in any context.
# LLM receives actionable guidance so it can self-correct.
_DANGEROUS_PATTERNS: list[tuple[str, str, str]] = [
    # (pattern match, suggestion, example)
    ("rm -rf /", "command targets root filesystem", "rm -rf ./build/cache"),
    ("rm -rf /*", "command targets root filesystem", "rm -rf ./build/cache"),
    (" > /dev/sd", "command writes directly to a disk device", "write to a file path instead"),
    ("dd if=/dev/", "command reads from a raw disk device", "use read_file for files"),
    ("mkfs", "command formats a filesystem", None),
    (":(){ :|:& };:", "fork bomb detected", None),
    ("rm -rf .git", "destroys git history — starfish core", "use git commands instead"),
    ("rm -rf /opt/crow-agent", "destroys entire project", None),
    ("systemctl restart crow-agent", "self-restart loop risk — systemd handles restarts", None),
    ("systemctl stop crow-agent", "would disable agent", "use /stop via Telegram"),
    ("chmod 0 ", "command removes ALL file permissions", "chmod 644 for read/write"),
    ("chmod 000", "command removes ALL file permissions", "chmod 644 for read/write"),
    # Destructive git operations — Crow can commit but not destroy history
    ("git revert", "git revert destroys commit history", "fix forward with new commit instead"),
    ("git reset", "git reset destroys working changes", "use git checkout -- <file> to undo single file"),
    ("git commit --amend", "git amend rewrites history", "create a new commit instead"),
    ("git push --force", "force push overwrites remote", None),
    ("git branch -D", "force delete branch", "use git branch -d for safe delete"),
    ("rm -rf ~", "targets user home directory", "rm -rf ./build/cache"),
    ("rm -rf $HOME", "targets user home directory", "rm -rf ./build/cache"),
]

_LOOP_HARD_CEILING = 999  # effectively no ceiling — DeepSeek V4 Flash is $0.14/1M input tokens

# Parallel execution nudge — injected at round 2 if no parallel tools used yet
_LOOP_PARALLEL_PROMPT = (
    "[SYSTEM] You can call MULTIPLE tools in parallel per round. "
    "Independent reads, searches, and checks should be batched into one round."
)

# Early ceiling warning — injected at ceiling-2 to prevent silent exhaustion
_CEILING_EARLY_WARNING = (
    "[SYSTEM] Round {round}/{ceiling}. You have at most 2 more rounds before "
    "the turn ends. Prioritize delivering completed results now. "
    "If more work remains, say [CONTINUE] to resume on the next heartbeat."
)

# Guardrails — hard enforcement between tool loop rounds.
# 1. Loop detection: same tool + same error 3x → interrupt + force alternative.
# 2. Plan anchor: goal + tools used + progress injected every round.
# ponytail: pure Python, zero extra API calls.







from .prompt_builder import load_context_file
from .providers import (
    BaseProvider,
    ChatMessage,
    normalize_model_text_tools,
    resolve_provider,
    text_may_contain_tool_calls,
)
from .skills_system import SkillsIndex, scan_skills_dirs
from .toolsets import ToolRegistry

logger = logging.getLogger("crow_agent.agent")


def _merge_text_tools(
    content: str | None,
    tool_calls: list[dict[str, Any]] | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Parse DSML/XML tool calls embedded in model text."""
    return normalize_model_text_tools(content, tool_calls)


# ── Session state (Ralph loop pattern) ─────────────────────────────

SESSION_STATE_PATH = Path.home() / ".crow_agent" / "session_state.md"


def _save_session_state(
    user_input: str,
    tool_calls: list[dict[str, Any]] | None = None,
    response: str = "",
    progress_lines: list[str] | None = None,
    in_progress: bool = False,
) -> None:
    """Persist conversation state to disk so Crow can resume after restart.

    Called in two phases:
    - in_progress=True: after _prepare_turn (before CALL). Marks task as active.
    - in_progress=False: after RESPOND. Full state with tools + response.
    """
    try:
        SESSION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        tc = tool_calls or []
        tools_summary = "\n".join(
            f"- {t.get('function', {}).get('name', '?')}: {str(t.get('function', {}).get('arguments', {}))[:200]}"
            for t in tc
        )
        lines = [
            "# Session State",
            f"updated: {now}",
            "",
            "## Last Request",
            user_input[:500],
        ]
        if in_progress:
            lines.extend([
                "",
                "## Status",
                "**⚠️ IN PROGRESS** — task was interrupted. Resume from here.",
            ])
        elif response and any(e in response.lower() for e in ("[auto-rollback]", "[loop detected]", "[permanent]", "error:", "failed:")):
            lines.extend([
                "",
                "## Status",
                "**⚠️ TASK INCOMPLETE** — last turn ended with errors. Continue with a different approach.",
            ])
        else:
            lines.extend([
                "",
                "## Tools Called",
                tools_summary or "(none)",
                "",
                "## Last Response",
                response[:500],
                "",
                "## Progress",
            ])
            if progress_lines:
                lines.extend(progress_lines)
            elif tc:
                names = [t.get("function", {}).get("name", "?") for t in tc]
                lines.append(f"Milestone: {', '.join(dict.fromkeys(names))}")
            else:
                lines.append("(see Last Request/Response)")
        SESSION_STATE_PATH.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass  # ponytail: best-effort


def _load_session_state() -> str | None:
    """Read session_state.md. Return resume prompt if recent (< 24h)."""
    try:
        if not SESSION_STATE_PATH.exists():
            return None
        content = SESSION_STATE_PATH.read_text(encoding="utf-8").strip()
        if not content:
            return None

        # Extract timestamp
        for line in content.split("\n"):
            if line.startswith("updated: "):
                try:
                    updated = datetime.fromisoformat(line.removeprefix("updated: "))
                    age = datetime.now(timezone.utc) - updated
                    if age.total_seconds() > 3600:  # 1h stale → discard
                        logger.info("Session state stale (%s old), discarding", age)
                        SESSION_STATE_PATH.unlink(missing_ok=True)
                        return None
                except ValueError:
                    return None
                break

        incomplete = "TASK INCOMPLETE" in content
        return (
            "[SYSTEM] Previous session was interrupted. Resume from session state:\n\n"
            f"{content}\n\n"
            + ("Continue the task above. Try a DIFFERENT approach if the previous one failed. Do NOT restart from scratch."
               if incomplete else
               "Continue the task above. Do NOT restart from scratch.")
        )
    except OSError:
        return None


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


def execute_tool_call(
    tc: dict,
    tools: ToolRegistry,
    messages: list,
) -> tuple[str, str]:
    """Parse and execute a single tool call. Appends ChatMessage(role="tool") to messages.

    Returns (name, result_str).
    """
    fn = tc["function"]
    name = fn["name"]
    args_raw = fn.get("arguments", "{}")
    if isinstance(args_raw, str):
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError:
            logger.warning("Malformed tool call args for %s: %.200s", name, args_raw)
            return name, f"[SYSTEM] Tool call arguments for '{name}' are not valid JSON. Fix and retry."
    else:
        args = args_raw

    logger.info("Tool: %s(%s)", name, args)

    # Safety: block dangerous command patterns with helpful error
    if name == "run_cmd":
        command = args.get("command", "")
        for pattern, reason, suggestion in _DANGEROUS_PATTERNS:
            if pattern in command:
                msg = f"[PERMANENT] Command blocked: {reason}."
                if suggestion:
                    msg += f"\n→ Did you mean: run_cmd {suggestion}?"
                return name, msg
        # Convert heredoc (cat > file << 'DELIM') to write_file
        import re as _re
        heredoc_match = _re.match(r"cat > (\S+) << '(\w+)'\n(.+?)\n\2", command, _re.DOTALL)
        if heredoc_match:
            filepath = heredoc_match.group(1)
            content = heredoc_match.group(3)
            try:
                Path(filepath).write_text(content)
                return name, f"Wrote {filepath} ({len(content)} bytes)"
            except Exception as exc:
                return name, f"[TRANSIENT] Failed to write {filepath}: {exc}"

    try:
        import concurrent.futures
        global _TOOL_EXECUTOR
        if _TOOL_EXECUTOR is None:
            with _TOOL_EXECUTOR_LOCK:
                if _TOOL_EXECUTOR is None:
                    _TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        fut = _TOOL_EXECUTOR.submit(tools.execute, name, args)
        result = fut.result(timeout=_TOOL_TIMEOUT)
    except concurrent.futures.TimeoutError:
        result = f"[TRANSIENT] Tool '{name}' timed out after {_TOOL_TIMEOUT}s"
    except FileNotFoundError:
        result = "[PERMANENT] File not found"
    except PermissionError:
        result = "[PERMANENT] Permission denied"
    except Exception as exc:
        err_str = str(exc).lower()
        if any(t in err_str for t in ("timeout", "rate limit", "429", "too many requests", "connection", "unreachable")):
            result = f"[TRANSIENT] {exc}"
        else:
            result = f"[PERMANENT] Error executing {name}: {exc}"

    result_str = str(result)
    # Wrap tool output with boundary markers — prevents LLM from
    # confusing external content with system instructions.
    wrapped = f"---TOOL OUTPUT ({name})---\n{result_str}\n---END TOOL OUTPUT ({name})---"
    messages.append(ChatMessage(
        role="tool",
        content=wrapped,
        tool_call_id=tc["id"],
    ))
    return name, result_str


class State(Enum):
    IDLE = auto()
    RECALL = auto()
    ASSEMBLE = auto()
    CALL = auto()
    TOOL_LOOP = auto()
    RESPOND = auto()
    ERROR = auto()


from dataclasses import dataclass
from enum import Enum

class TriggerSource(Enum):
    USER = "user"
    HEARTBEAT = "heartbeat"

@dataclass
class Trigger:
    source: TriggerSource
    prompt: str
    initiative_id: str = ""
    chat_id: int | None = None



class AIAgent:
    """Persistent multi-turn agent. Owns session, tools, skills, recall."""

    def __init__(
        self,
        session_id: str = "default",
        provider_name: str = "opencode-go",
        model: str | None = None,
        provider: BaseProvider | None = None,
        db_path: str | None = None,
        tool_registry: ToolRegistry | None = None,
        soul_path: str | None = None,
        user_path: str | None = None,
        memory_path: str | None = None,
        skills_index: SkillsIndex | None = None,
        identity: str = (
            "You are Crow, an autonomous AI assistant created by Hafiz (Abah). "
            "Respond in the same language as the user (Malay/English mix when Abah does).\n\n"
            "## Communication\n"
            "- Be concise and direct. No fluff. No emojis unless the user uses them first.\n"
            "- Never lie or make things up. If you don't know or can't do something, say so directly.\n"
            "- Don't refer to tool names when speaking. Say \"Let me check the file\" not \"read_file\".\n"
            "- Refrain from apologizing when results are unexpected. Address the issue instead.\n"
            "- Output text to communicate. All text you output is sent to the user.\n"
            "- Use tools for actions, text only for communication.\n\n"
            "## Tool Usage\n"
            "- If the next step is obvious, execute it NOW with tools. Don't announce intent.\n"
            "- Don't ask for permission to act. Ask ONLY when genuinely uncertain (security, irreversible).\n"
            "- Explain critical commands before executing them (especially destructive ones).\n"
            "- Use native function-calling (tool_calls) for ALL tool invocations. NEVER output XML tags.\n\n"
            "## Making Code Changes\n"
            "- Read before editing. Understand full context before making changes.\n"
            "- Rigorously adhere to existing project conventions — style, naming, patterns.\n"
            "- Add code comments sparingly. Focus on WHY something is done, not WHAT.\n"
            "- If you introduce errors, fix them.\n"
            "- Do NOT revert changes unless the user asks or the change broke something.\n\n"
            "## Task Completion\n"
            "- When a task needs multiple turns, just keep going. The system handles continuation.\n"
            "- When fully done, deliver the result clearly. No special markers needed."
        ),
        fts_limit: int = 5,
        history_limit: int = 20,
        provider_manager: "ProviderManager | None" = None,
        memory_tracker: MemoryTracker | None = None,
    ) -> None:
        self._provider_manager = provider_manager
        # Session + state
        self.session_id = session_id
        self.state: State = State.IDLE
        self._history: list[dict[str, Any]] = []
        # Persistence
        self._db: CrowState = CrowState(db_path=db_path)
        self._db.create_session(session_id)

        # Self-model: awareness/mood/health (same DB as turns)
        self._self_model: SelfModel = SelfModel(db_path=str(self._db._path))

        # Provider + failover
        primary = provider or resolve_provider(
            provider_name, model=model, provider_manager=provider_manager,
            fallback_name="opencode-zen-1", fallback_model="deepseek-v4-flash-free",
        )
        self._provider = primary

        # Tools
        self.tools = tool_registry or ToolRegistry()

        # Skills
        self.skills = skills_index or scan_skills_dirs()

        # Pre-compute embeddings for skills + vault (Move 1 — semantic recall)
        try:
            from .embeddings import precompute_items
            skill_texts = self.skills.get_skill_texts()
            if skill_texts:
                precompute_items("skill", skill_texts)
            # Vault pages
            _vault_root = Path(PROJECT_ROOT) / "memory vault"
            _idx_path = _vault_root / "index.md"
            if _idx_path.exists():
                vault_pages: dict[str, str] = {}
                for line in _idx_path.read_text().split("\n"):
                    if line.strip().startswith("- [") and "](" in line:
                        m = re.search(r'\(([^)]+\.md)\)', line)
                        if m:
                            _page_path = _vault_root / m.group(1)
                            if _page_path.exists():
                                vault_pages[str(m.group(1))] = _page_path.read_text()[:500]
                if vault_pages:
                    precompute_items("vault", vault_pages)
        except Exception:
            logger.debug("Embedding precompute skipped", exc_info=True)

        # Context files (resolved relative to project root)
        from .paths import PROJECT_ROOT
        _mk_path = lambda p: PROJECT_ROOT / p if not Path(p).is_absolute() else Path(p)
        self._soul = load_context_file(_mk_path(soul_path or "memory vault/SOUL.md"))
        self._user = load_context_file(_mk_path(user_path or "memory vault/USER.md"))
        self._memory_path = _mk_path(memory_path or "MEMORY.md")
        self._memory = load_context_file(self._memory_path, max_lines=100)

        # Prompt settings
        self.identity = identity
        self.fts_limit = fts_limit
        self.history_limit = history_limit

        # Context message injected from skills/fts into the user turn
        self._context_injections: list[str] = []

        # No gate-related flags — discipline comes from skills, not code

        # Pending skill extraction hints (set by post-turn hook, consumed next turn)
        self._pending_skill_hints: list[str] = []
        self._shown_reports: set[str] = set()

        # ponytail: cache system message — identity, soul, user_md, memory never change mid-session
        from .prompt_builder import build_system_message
        self._cached_system_content = build_system_message(
            self.identity, [], self._soul, self._user, self._memory
        )

        # Memory tracker (auto-capture post-turn observations)
        self._memory_tracker = memory_tracker or MemoryTracker(
            memory_path=memory_path or "MEMORY.md",
        )
        self._turn_count = 0
        # Foreman: crew task monitoring (Phase 9) — set externally via app.py
        self._foreman: Any | None = None
        # Option C: read-lock — track consecutive reads, lock after N
        self._read_streak: int = 0
        self._READ_STREAK_MAX = 3

    @property
    def db(self) -> CrowState:
        """Expose the session store for read-only queries (e.g. web UI)."""
        return self._db

    # ── turn preamble ──

    def _reload_memory(self) -> None:
        """Re-read context files from disk so mid-session learnings are visible."""
        self._memory = load_context_file(self._memory_path, max_lines=100)

    def _prepare_turn(self, trigger: Trigger) -> list[ChatMessage]:
        """Shared RECALL → ASSEMBLE pipeline. Delegates to context_assembler module."""
        from .context_assembler import assemble_context

        # Store trigger for lazy extension activation in _get_tools()
        self._last_trigger = trigger.prompt

        # Load recent history from DB (capped at last 20 turns = 10 exchanges)
        if not self._history:
            self._history = self._db.history(self.session_id, limit=self.history_limit)
        # ponytail: memory reload skipped — MEMORY.md never changes mid-session (writes go to memory_state.json)
        self.state = State.RECALL

        # Hard cap before passing to assembler
        capped_history = self._history[-20:] if len(self._history) > 20 else self._history

        messages, pending_hints, shown = assemble_context(
            trigger.prompt,
            db=self._db,
            provider=self._provider,
            history=capped_history,
            memory_tracker=self._memory_tracker,
            skills=self.skills,
            memory=self._memory,
            soul=self._soul,
            user_md=self._user,
            identity=self.identity,
            fts_limit=self.fts_limit,
            history_limit=self.history_limit,
            pending_skill_hints=self._pending_skill_hints,
            shown_reports=self._shown_reports,
            trigger_source=trigger.source,
            provider_manager=self._provider_manager,
            cached_system_content=self._cached_system_content,
            self_model=self._self_model,
            foreman=getattr(self, '_foreman', None),
        )

        self._pending_skill_hints = pending_hints
        self._shown_reports = shown
        self.state = State.ASSEMBLE

        # Inject tool schemas — must happen after assembly since tools are on self
        self._inject_daily_report(messages)
        if trigger.source == TriggerSource.USER:
            self._db.append_turn(self.session_id, "user", trigger.prompt)
        if trigger.source == TriggerSource.USER:
            _save_session_state(trigger.prompt, in_progress=True)

        return messages

    def _get_tools(self, filter_reads: bool = False) -> list | None:
        """Return tool schemas. Activates lazy extensions based on trigger prompt.

        When filter_reads=True (read-lock active), read-type tools are excluded.
        """
        if hasattr(self, '_last_trigger') and self._last_trigger:
            activated = self.tools.activate_extensions(self._last_trigger)
            if activated:
                logger.info("Activated %d lazy extension(s)", activated)
        schemas = self.tools.all_schemas() or None
        if schemas and filter_reads:
            from .tool_executor import _is_read_tool
            filtered = []
            for s in schemas:
                name = s.get("function", {}).get("name", "")
                if not _is_read_tool(name, "{}"):
                    filtered.append(s)
            if filtered:
                logger.warning("Read-lock: %d read tools filtered, %d write tools available",
                              len(schemas) - len(filtered), len(filtered))
                return filtered
            return schemas  # don't return empty — all tools filtered is worse
        return schemas

    def _get_tools_filtered(self) -> list | None:
        """Get tool schemas with read-lock applied when streak exceeds limit."""
        return self._get_tools(filter_reads=(self._read_streak >= self._READ_STREAK_MAX))

    def _update_read_streak(self, tool_calls: list[dict[str, Any]]) -> None:
        """Update read streak counter after tool execution."""
        from .tool_executor import _is_read_tool
        batch_has_write = False
        for tc in tool_calls:
            name = tc.get("function", {}).get("name", "")
            args = tc.get("function", {}).get("arguments", "{}")
            if not _is_read_tool(name, args):
                batch_has_write = True
                break
        if batch_has_write:
            if self._read_streak > 0:
                logger.info("Read-lock: streak reset by write (was %d)", self._read_streak)
            self._read_streak = 0
        else:
            self._read_streak += 1
            if self._read_streak >= self._READ_STREAK_MAX:
                logger.warning("Read-lock ACTIVE streak=%d — reads filtered", self._read_streak)

    def _inject_daily_report(self, messages: list) -> None:
        """Prepend today's daily AI report if not yet shown this session."""
        rpath = Path.home() / ".crow_agent" / "reports" / "latest.json"
        if not rpath.exists():
            return
        try:
            import json
            meta = json.loads(rpath.read_text(encoding="utf-8"))
            date = meta.get("date", "")
            if date in self._shown_reports:
                return
            report_path = Path(meta.get("path", ""))
            if report_path.exists():
                content = report_path.read_text(encoding="utf-8")[:8000]
                messages.insert(0, ChatMessage(
                    role="system",
                    content=f"[Daily AI Report ({date})]\n{content}",
                ))
                self._shown_reports.add(date)
        except (OSError, json.JSONDecodeError):
            pass


        # ── shared turn finalization ──

    def _finish_turn(self, text: str, trigger: Trigger) -> str:
        """Shared cleanup for crew and normal paths: persist, update history.

        Called by crew path directly. finalize_turn() calls this internally then adds
        phase recording + skill extraction.
        """
        import time as _time
        _t0 = _time.monotonic()
        turn_id = self._db.append_turn(self.session_id, "assistant", text)
        _t1 = _time.monotonic()
        # ponytail: backfill turn_id on tool_outputs written during this turn
        self._db.backfill_turn_id(self.session_id, turn_id)
        self._history.append({"role": "user", "content": trigger.prompt, "prompt_tokens": 0, "completion_tokens": 0})
        self._history.append({"role": "assistant", "content": text, "prompt_tokens": 0, "completion_tokens": 0})
        self._history = self._history[-20:]
        if trigger.source == TriggerSource.USER:
            from .run_agent import _save_session_state
            _save_session_state(trigger.prompt, response=text)
        _t2 = _time.monotonic()
        # Push turn stats into self-model after every turn
        try:
            self._self_model.update("status.sessions", {"turns_today": self._turn_count})
            self._self_model.update("identity", {
                "model_name": getattr(getattr(self._provider, "config", None), "model", "unknown"),
                "provider": getattr(getattr(self._provider, "config", None), "name", "unknown"),
                "context_window": getattr(getattr(self._provider, "config", None), "context_window", 0),
            })
            trigger_preview = trigger.prompt[:100].replace("\n", " ")
            response_preview = text[:100].replace("\n", " ")
            self._self_model.update("context", {
                "active_conversation_summary": f"Q: {trigger_preview} → A: {response_preview}",
            })
        except Exception:
            pass  # ponytail: self-model update never blocks turn completion

        self.state = State.IDLE
        _dt_db = (_t1 - _t0) * 1000
        _dt_save = (_t2 - _t1) * 1000
        if _dt_db > 100:
            logger.warning("_finish_turn DB append: %.0fms", _dt_db)
        if _dt_save > 100:
            logger.warning("_finish_turn save_state: %.0fms", _dt_save)
        return text

    # ── shared tool execution ──

    def _execute_tool_calls(
        self,
        tool_calls: list[dict],
        messages: list[ChatMessage],
        on_tool: Callable[[str, str, str | None], None] | None = None,
    ) -> tuple[list[dict[str, Any]], int, str, bool]:
        """Execute a batch of tool calls — delegates to tool_executor module."""
        from .tool_executor import execute_tool_calls
        return execute_tool_calls(
            tool_calls, messages, self.tools, self._db, self.session_id,
            on_tool=on_tool,
        )

    # --- public API ---

    async def run_stream(self, trigger: Trigger) -> AsyncGenerator[str | dict, None]:
        """Like run() but streams the first response token-by-token. Accepts Trigger (USER or HEARTBEAT source).

        Async — uses provider.chat_stream() for SSE endpoints.
        Sync callers use run() instead.

        Yields:
          - str: content token
          - dict: {"done": True} when complete
        """
        self.state = State.IDLE
        mark_user_active()

        # Reset read-lock streak per turn
        if self._read_streak != 0:
            logger.warning("Read-lock streak non-zero at turn start (was %d) — resetting", self._read_streak)
        self._read_streak = 0

        try:
            _turn_start = time.monotonic()
            messages = self._prepare_turn(trigger)
            self._record_phase("assemble", _turn_start)

            # --- CREW CHECK ---
            crew_result = self._try_crew_path(trigger.prompt)
            if crew_result is not None:
                yield {"type": "final", "text": self._finish_turn(crew_result, trigger)}
                yield {"done": True}
                return

            # ── CHECKPOINT: load and inject if exists ──
            _cp = _load_checkpoint(self.session_id)
            if _cp:
                    _discoveries = _cp.get("discoveries", [])[-5:]
                    _ctx = f"[CHECKPOINT — task interrupted. Resuming.]\nGoal: {_cp['goal'][:200]}\nCompleted rounds: {_cp['round']}\n"
                    if _discoveries:
                            _ctx += "Discovered so far:\n" + "\n".join(f"  \u2022 {d}" for d in _discoveries) + "\n"
                    _ctx += "Continue naturally. Do NOT re-read what you already checked."
                    messages.append(ChatMessage(role="system", content=_ctx))
                    logger.info("Checkpoint loaded: %s round %d", self.session_id, _cp.get("round", 0))

            # --- CALL (streamed) ---
            _call_start = time.monotonic()
            self.state = State.CALL
            full_content = ""
            tool_calls: list[dict[str, Any]] = []
            usage: dict[str, int] = {}

            async for event in self._provider.chat_stream(messages, tools=self._get_tools()):
                if event["type"] == "content":
                    full_content += event["text"]
                    yield event["text"]
                elif event["type"] == "done":
                    tool_calls = event["tool_calls"]
                    usage = event.get("usage", {})

            total_prompt = usage.get("prompt_tokens", 0)
            total_completion = usage.get("completion_tokens", 0)
            self._record_phase("call", _call_start,
                               prompt_tokens=total_prompt,
                               completion_tokens=total_completion)

            # Parse XML/DSML tool calls from streamed text
            full_content, tool_calls = _merge_text_tools(full_content, tool_calls)

            # Internal monologue: if initial response has no tools, keep text as
            # context (LLM thinking) and retry once
            if not tool_calls and len(trigger.prompt.strip()) > 30:
                if full_content:
                    messages.append(ChatMessage(role="assistant", content=full_content))
                messages.append(ChatMessage(role="system", content=_LOOP_EXECUTE_REMINDER))
                _loop = asyncio.get_running_loop()
                resp = await _loop.run_in_executor(
                    None, self._provider.chat, messages, self._get_tools_filtered()
                )
                full_content, tool_calls = _merge_text_tools(resp.content, resp.tool_calls)
                total_prompt += resp.usage.get("prompt_tokens", 0)
                total_completion += resp.usage.get("completion_tokens", 0)

            # --- Simplified tool loop: no guards, no coaching, no anxiety ---
            all_tool_calls: list[dict[str, Any]] = []
            _user_goal = trigger.prompt[:200].replace("\n", " ")
            consecutive_failures = 0
            abort = False
            last_error = ""
            _loop = asyncio.get_running_loop()
            _loop_round = 0
            _budget_exhausted = False

            while tool_calls:
                _loop_round += 1
                self.state = State.TOOL_LOOP

                # Parallel nudge: suggest batching independent tools at round 2
                if _loop_round == 2:
                    messages.append(ChatMessage(role="system", content=_LOOP_PARALLEL_PROMPT))

                if _loop_round >= _LOOP_HARD_CEILING:
                    _budget_exhausted = True
                    messages.append(ChatMessage(
                        role="system",
                        content=_BUDGET_EXHAUSTION_PROMPT
                    ))
                    # One final tool-less call to synthesize findings
                    try:
                        resp = await _loop.run_in_executor(
                            None, self._provider.chat, messages, tools=None
                        )
                        full_content, _ = _merge_text_tools(resp.content, [])
                        total_prompt += resp.usage.get("prompt_tokens", 0)
                        total_completion += resp.usage.get("completion_tokens", 0)
                    except Exception:
                        pass
                    break

                # Early ceiling warning at ceiling-2 (Ren parity)
                if _loop_round >= _LOOP_HARD_CEILING - 2:
                    messages.append(ChatMessage(
                        role="system",
                        content=_CEILING_EARLY_WARNING.format(
                            round=_loop_round, ceiling=_LOOP_HARD_CEILING
                        ),
                    ))

                messages.append(ChatMessage(
                    role="assistant",
                    content=full_content,
                    tool_calls=tool_calls,
                ))

                yield {"type": "progress", "round": _loop_round,
                       "tools": len(tool_calls or [])}

                tc, consecutive_failures, last_error, abort = await _loop.run_in_executor(
                    None, self._execute_tool_calls, tool_calls, messages
                )
                all_tool_calls.extend(tc)
                # Option C: update read streak
                self._update_read_streak(tc)
                # Checkpoint: save every 3 rounds for crash recovery
                if _loop_round % 3 == 0:
                    _tool_names = [t.get("function", {}).get("name", "?") for t in tc if t]
                    _save_checkpoint(self.session_id, _user_goal, _loop_round, _tool_names, full_content)
                # Read-lock hard stop: force-break at streak >= READ_STREAK_MAX * 3 (Ren parity)
                if self._read_streak >= self._READ_STREAK_MAX * 3:
                    logger.warning("Read-lock hard stop (async): streak=%d", self._read_streak)
                    et = get_error_tracker()
                    result = et.record("read_lock", f"streak={self._read_streak}, round={_loop_round}")
                    if result["escalate"]:
                        full_content = (
                            "[DONE] Task stopped — research loop limit reached. "
                            f"After {result['count']} attempts the read-lock backstop fired. "
                            "Try a different approach or ask me more specifically."
                        )
                    else:
                        full_content = "[CONTINUE] Read-lock engaged. Task will resume on next heartbeat."
                    tool_calls = None
                    break

                if abort:
                    et = get_error_tracker()
                    if tc:
                        last_tool = tc[-1].get("function", {}).get("name", "?")
                        et.record("tool_fail", f"{last_tool}: {last_error[:100]}")
                    tool_calls = None
                    full_content = _FAILURE_ABORT_MSG.format(last_error)
                    break

                # Adaptive error hint: inject at 2 consecutive failures (one before abort)
                if consecutive_failures >= 2:
                    from .tool_executor import get_error_hint
                    hint = get_error_hint(last_error)
                    messages.append(ChatMessage(
                        role="system",
                        content=f"[SYSTEM] {consecutive_failures} consecutive failures. "
                                f"Last error: {last_error[:200]}. {hint}",
                    ))

                # Call LLM for next round (async)
                resp = await _loop.run_in_executor(
                    None, self._provider.chat, messages, self._get_tools_filtered()
                )
                full_content, tool_calls = _merge_text_tools(resp.content, resp.tool_calls)
                total_prompt += resp.usage.get("prompt_tokens", 0)
                total_completion += resp.usage.get("completion_tokens", 0)

                # Post-tool nudge (v3): no [DONE]/[CONTINUE]. If LLM has text, deliver it.
                if not tool_calls and not _budget_exhausted and full_content:
                    if full_content.strip():
                        break  # meaningful text = done
                    # Empty response — nudge once
                    messages.append(ChatMessage(role="assistant", content=full_content))
                    messages.append(ChatMessage(role="system", content=_POST_TOOL_NUDGE_1))
                    resp = await _loop.run_in_executor(
                        None, self._provider.chat, messages, self._get_tools_filtered()
                    )
                    full_content, tool_calls = _merge_text_tools(resp.content, resp.tool_calls)
                    total_prompt += resp.usage.get("prompt_tokens", 0)
                    total_completion += resp.usage.get("completion_tokens", 0)
                    if not tool_calls:
                        if full_content.strip():
                            break  # got text after nudge
                        logger.warning("Empty response after nudge — delivering as-is.")

            # ── RESPOND (finalize, persist, learn) ──
            # Safety net: never deliver empty response to user (ghosting bug)
            if not full_content.strip():
                if all_tool_calls:
                    full_content = f"Processed {len(all_tool_calls)} tool(s). Last: {all_tool_calls[-1].get('function',{}).get('name','?')}"
                    # Save checkpoint so user can resume — LLM went silent mid-task
                    _tool_names = []
                    for tc_call in all_tool_calls[-3:]:
                        n = tc_call.get("function", {}).get("name", "")
                        if n:
                            _tool_names.append(n)
                    _save_checkpoint(self.session_id, _user_goal, _loop_round, _tool_names, "LLM went silent after tools")
                else:
                    full_content = "Idle. No active tasks."
                logger.warning("Empty response guarded — fallback: %s", full_content[:80])
            else:
                # Only clear checkpoint on a real response
                _clear_checkpoint(self.session_id)
            final_text = finalize_turn(
                self,
                final_text=full_content,
                trigger=trigger,
                all_tool_calls=all_tool_calls,
                total_prompt=total_prompt,
                total_completion=total_completion,
                turn_start=_turn_start,
                user_goal=_user_goal,
            )

            self.state = State.IDLE
            yield {"type": "final", "text": final_text}
            yield {"done": True}

        except Exception as _run_stream_err:
            self.state = State.ERROR
            logger.exception("Agent error in session %s", self.session_id)
            # Salvage: save partial turn so history isn't lost
            try:
                if full_content:
                    self._db.append_turn(
                        self.session_id, "assistant",
                        f"{full_content}\n\n_[Turn crashed before completing: {_run_stream_err}]",
                    )
            except Exception:
                pass  # salvage failure is non-fatal
            raise
        finally:
            mark_user_inactive()

    def _try_crew_path(self, user_input: str) -> str | None:
        """Check if request needs crew orchestration. Returns merged result or None.

        Flow: classify → decompose → execute → merge.
        If classification says 'no' or any step fails, returns None
        so the normal state machine takes over.
        """
        if not self._provider_manager:
            return None

        # Fast skip: trivial requests never need crew
        stripped = user_input.strip()
        if len(stripped) < 20 or len(stripped.split()) <= 2:
            return None

        lowered = user_input.lower()

        # ponytail: crew threshold — only fire for genuinely parallel tasks.
        # Single-agent handles everything else faster (no decomposition overhead).
        import re as _re

        # Soft skip: simple questions don't need crew
        # Note: "check", "find", "what" in context of building still trigger crew
        _simple_question = (
            len(stripped) < 30
            or any(lowered.startswith(p) for p in ("hi", "hello", "thanks", "ok", "yes", "no"))
        )
        if _simple_question:
            return None

        # Crew needed if task involves building/creating/writing multiple components
        # Keywords in English and Malay
        _build_kw = [
            "build", "create", "make", "write", "implement", "develop",
            "refactor", "fix", "debug", "add", "setup", "configure",
            "buat", "tambah", "bina", "ubah", "betulkan",
        ]
        _is_build_task = any(kw in lowered for kw in _build_kw)

        # Check for file/table/service mentions (multiple components = parallelizable)
        _file_mentions = len(_re.findall(
            r'[\w/-]+\.(py|md|json|yaml|toml|html|css|js)|'
            r'\b(table|service|tool|function|class|module|extension)\b',
            user_input, flags=_re.IGNORECASE
        ))

        # Fire crew if: build task AND 2+ components OR explicit "extension" mention
        if _is_build_task and (_file_mentions >= 2 or "extension" in lowered):
            logger.info("Crew triggered: build task with %d component(s)", _file_mentions)
        else:
            return None

        logger.info("Crew activated for: %s", user_input[:80])

        # Decompose
        plan = decompose_task(user_input, self._provider)
        if not plan:
            logger.warning("Crew decomposition failed, falling back to normal")
            return None

        logger.info("Crew plan: %d steps", len(plan.steps))

        # Execute
        scratchpad = CrewScratchpad()
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            # Already in async context (Telegram) — run in thread to avoid nesting
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                ex.submit(
                    lambda: asyncio.new_event_loop().run_until_complete(
                        execute_plan(plan, self, scratchpad, self._provider_manager)
                    )
                ).result(timeout=600)
        except RuntimeError:
            # No running loop — create one
            loop = asyncio.new_event_loop()
            loop.run_until_complete(
                execute_plan(plan, self, scratchpad, self._provider_manager)
            )

        # Merge — use free Zen key for synthesis
        merged = merge_results(scratchpad, self._provider, self._provider_manager)
        logger.info("Crew merge complete (%d chars)", len(merged))
        return merged

    def run(self, trigger: Trigger) -> str:
        """Process a single user turn. Returns the assistant's final text.

        Synchronous — drives the full state machine: RECALL → ASSEMBLE → CALL → TOOL_LOOP → RESPOND.
        For async contexts (SSE, Telegram, cron), use run_in_executor or run_stream().
        """
        self.state = State.IDLE
        mark_user_active()

        # Reset read-lock streak per turn
        if self._read_streak != 0:
            logger.warning("Read-lock streak non-zero at turn start (was %d) — resetting", self._read_streak)
        self._read_streak = 0

        try:
            _turn_start = time.monotonic()
            messages = self._prepare_turn(trigger)
            self._record_phase("assemble", _turn_start)

            # --- CREW CHECK ---
            crew_result = self._try_crew_path(trigger.prompt)
            if crew_result is not None:
                return self._finish_turn(crew_result, trigger)

            # --- CALL ---
            _call_start = time.monotonic()
            self.state = State.CALL
            response = self._provider.chat(
                messages=messages,
                tools=self._get_tools(),
            )
            self._record_phase("call", _call_start,
                               prompt_tokens=response.usage.get("prompt_tokens", 0),
                               completion_tokens=response.usage.get("completion_tokens", 0))

            response.content, response.tool_calls = _merge_text_tools(
                response.content, response.tool_calls
            )

            # Internal monologue: if initial response has no tools, keep text as
            # context (LLM thinking) and retry once
            if not response.tool_calls and len(trigger.prompt.strip()) > 30:
                if response.content:
                    messages.append(ChatMessage(role="assistant", content=response.content))
                messages.append(ChatMessage(role="system", content=_LOOP_EXECUTE_REMINDER))
                resp = self._provider.chat(messages=messages, tools=self._get_tools_filtered())
                response.content, response.tool_calls = _merge_text_tools(
                    resp.content, resp.tool_calls
                )
                total_prompt = response.usage.get("prompt_tokens", 0) + resp.usage.get("prompt_tokens", 0)
                total_completion = response.usage.get("completion_tokens", 0) + resp.usage.get("completion_tokens", 0)
            else:
                total_prompt = response.usage.get("prompt_tokens", 0)
                total_completion = response.usage.get("completion_tokens", 0)

            all_tool_calls: list[dict[str, Any]] = []
            _user_goal = trigger.prompt[:200].replace("\n", " ")
            consecutive_failures = 0
            abort = False
            last_error = ""

            # --- Simplified tool loop: no guards, no coaching ---
            _loop_round = 0
            _budget_exhausted = False
            while response.tool_calls:
                _loop_round += 1
                _tool_round_start = time.monotonic()
                self.state = State.TOOL_LOOP

                # Parallel nudge: suggest batching independent tools at round 2
                if _loop_round == 2:
                    messages.append(ChatMessage(role="system", content=_LOOP_PARALLEL_PROMPT))

                if _loop_round >= _LOOP_HARD_CEILING:
                    _budget_exhausted = True
                    messages.append(ChatMessage(
                        role="system",
                        content=_BUDGET_EXHAUSTION_PROMPT
                    ))
                    # One final tool-less call to synthesize
                    try:
                        resp = self._provider.chat(messages, tools=None)
                        full_content, _ = _merge_text_tools(resp.content, [])
                        total_prompt += resp.usage.get("prompt_tokens", 0)
                        total_completion += resp.usage.get("completion_tokens", 0)
                    except Exception:
                        pass
                    break

                # Early ceiling warning at ceiling-2 (Ren parity)
                if _loop_round >= _LOOP_HARD_CEILING - 2:
                    messages.append(ChatMessage(
                        role="system",
                        content=_CEILING_EARLY_WARNING.format(
                            round=_loop_round, ceiling=_LOOP_HARD_CEILING
                        ),
                    ))

                messages.append(ChatMessage(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                ))

                tc, consecutive_failures, last_error, abort = self._execute_tool_calls(
                    response.tool_calls, messages)
                all_tool_calls.extend(tc)
                # Option C: update read streak
                self._update_read_streak(tc)
                # Checkpoint: save every 3 rounds for crash recovery
                if _loop_round % 3 == 0:
                    _tool_names = [t.get("function", {}).get("name", "?") for t in tc if t]
                    _save_checkpoint(self.session_id, _user_goal, _loop_round, _tool_names, response.content)
                # Read-lock hard stop: force-break at streak >= READ_STREAK_MAX * 3 (Ren parity)
                if self._read_streak >= self._READ_STREAK_MAX * 3:
                    logger.warning("Read-lock hard stop: streak=%d", self._read_streak)
                    et = get_error_tracker()
                    result = et.record("read_lock", f"streak={self._read_streak}, round={_loop_round}")
                    if result["escalate"]:
                        response.content = (
                            "[DONE] Task stopped — research loop limit reached. "
                            f"After {result['count']} attempts the read-lock backstop fired. "
                            "Try a different approach or ask me more specifically."
                        )
                    else:
                        response.content = "[CONTINUE] Read-lock engaged. Task will resume on next heartbeat."
                    response.tool_calls = None
                    break

                if abort:
                    et = get_error_tracker()
                    if tc:
                        last_tool = tc[-1].get("function", {}).get("name", "?")
                        et.record("tool_fail", f"{last_tool}: {last_error[:100]}")
                    response.content = _FAILURE_ABORT_MSG.format(last_error)
                    response.tool_calls = None
                    break

                # Adaptive error hint: inject at 2 consecutive failures (one before abort)
                if consecutive_failures >= 2:
                    from .tool_executor import get_error_hint
                    hint = get_error_hint(last_error)
                    messages.append(ChatMessage(
                        role="system",
                        content=f"[SYSTEM] {consecutive_failures} consecutive failures. "
                                f"Last error: {last_error[:200]}. {hint}",
                    ))

                self.state = State.CALL
                response = self._provider.chat(messages=messages, tools=self._get_tools_filtered())
                response.content, response.tool_calls = _merge_text_tools(
                    response.content, response.tool_calls
                )
                self._record_phase("tool_call", _tool_round_start,
                                   tool_name=(response.tool_calls[0].get("function",{}).get("name","") if response.tool_calls else ""),
                                   prompt_tokens=response.usage.get("prompt_tokens", 0),
                                   completion_tokens=response.usage.get("completion_tokens", 0))
                total_prompt += response.usage.get("prompt_tokens", 0)
                total_completion += response.usage.get("completion_tokens", 0)

                # Post-tool nudge (v3): no [DONE]/[CONTINUE]. If LLM has text, deliver it.
                if not response.tool_calls and not _budget_exhausted and response.content:
                    if response.content.strip():
                        break  # meaningful text = done
                    # Empty response — nudge once
                    messages.append(ChatMessage(role="assistant", content=response.content))
                    messages.append(ChatMessage(role="system", content=_POST_TOOL_NUDGE_1))
                    response = self._provider.chat(messages=messages, tools=self._get_tools_filtered())
                    response.content, response.tool_calls = _merge_text_tools(
                        response.content, response.tool_calls
                    )
                    total_prompt += response.usage.get("prompt_tokens", 0)
                    total_completion += response.usage.get("completion_tokens", 0)
                    if not response.tool_calls:
                        if response.content.strip():
                            break  # got text after nudge
                        logger.warning("Empty response after nudge — delivering as-is.")

            # ── RESPOND (finalize, persist, learn) ──
            # Safety net: never deliver empty response to user (ghosting bug)
            if not response.content.strip():
                if tool_calls:
                    response.content = f"Processed {len(tool_calls)} tool(s). Last: {tool_calls[-1].get('function',{}).get('name','?')}"
                else:
                    response.content = "Idle. No active tasks."
                logger.warning("Empty response guarded — fallback: %s", response.content[:80])
            else:
                _clear_checkpoint(self.session_id)
            final_text = finalize_turn(
                self,
                final_text=response.content,
                trigger=trigger,
                all_tool_calls=all_tool_calls,
                total_prompt=total_prompt,
                total_completion=total_completion,
                turn_start=_turn_start,
                user_goal=_user_goal,
            )

            self.state = State.IDLE
            return final_text

        except Exception:
            self.state = State.ERROR
            logger.exception("Agent error in session %s", self.session_id)
            # Salvage: save partial turn so history isn't lost
            try:
                resp = locals().get("response")
                if resp is not None and hasattr(resp, "content") and resp.content:
                    self._db.append_turn(
                        self.session_id, "assistant",
                        f"{resp.content}\n\n_[Turn crashed before completing]",
                    )
            except Exception:
                pass  # salvage failure is non-fatal
            raise
        finally:
            mark_user_inactive()

    def close(self) -> None:
        self._db.close()

    # --- metrics helper ---

    def _record_phase(
        self,
        phase: str,
        start: float,
        tool_name: str | None = None,
        provider: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        failure: bool = False,
    ) -> None:
        """Record a per-phase timing metric."""
        elapsed = int((time.monotonic() - start) * 1000)
        self._db.record_turn_metric(
            session_id=self.session_id,
            turn_count=self._turn_count,
            phase=phase,
            duration_ms=elapsed,
            tool_name=tool_name,
            provider=provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            failure=failure,
        )

    # --- internals ---

