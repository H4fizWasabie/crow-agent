"""Crew system — multi-agent orchestration with persistent worker memory.

Architecture (ADR 0004):
  Crow (orchestrator) → classify complexity → decompose task → execute plan
  → merge results → respond. Workers have their own SQLite sessions, per-profile
  provider keys, and communicate via an append-only markdown scratchpad.

Tight integration with existing:
  - agent_profiles.py: AgentProfile, run_child_task (worker execution)
  - crow_state.py: CrowState (persistent worker sessions)
  - provider_manager.py: ProviderManager (pool + fallback)
  - run_agent.py: AIAgent state machine (crew activation check)
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("crow_agent.crew")

# ── Scratchpad ────────────────────────────────────────────────────


def _cleanup_old_scratchpads(crew_dir: Path) -> None:
    """Delete scratchpad files older than 1 hour. Best-effort."""
    try:
        now = time.time()
        for f in crew_dir.glob("scratchpad_*.md"):
            if now - f.stat().st_mtime > 3600:  # 1 hour
                f.unlink(missing_ok=True)
    except OSError:
        pass


class CrewScratchpad:
    """Append-only markdown scratchpad shared by all workers in a crew run.

    Format:
        ## STEP: <step-id> | worker: <profile> | status: <pending|running|done>
        <output content>
        ## END

    Workers never read raw — they query via scripts (awk/grep).
    """

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            crew_dir = Path(tempfile.gettempdir()) / "crew"
            crew_dir.mkdir(parents=True, exist_ok=True)
            # TTL cleanup: delete scratchpad files older than 1 hour
            _cleanup_old_scratchpads(crew_dir)
            self.path = str(crew_dir / f"scratchpad_{secrets.token_hex(4)}.md")
        else:
            self.path = str(path)
        # Create empty file
        Path(self.path).touch()

    def append_step(self, step_id: str, worker: str, status: str, content: str) -> None:
        """Append a ## STEP block — safe for parallel workers."""
        block = (
            f"\n## STEP: {step_id} | worker: {worker} | status: {status}\n"
            f"{content}\n"
            f"## END\n"
        )
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(block)

    def query_done(self) -> list[str]:
        """Return completed step blocks (status: done) for merge."""
        import subprocess
        try:
            result = subprocess.run(
                ["awk", '/## STEP:.*status: done/,/## END/', self.path],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return []
            # Split into individual step blocks
            blocks = []
            current: list[str] = []
            for line in result.stdout.split("\n"):
                if line.startswith("## STEP:") and current:
                    blocks.append("\n".join(current))
                    current = [line]
                else:
                    current.append(line)
            if current:
                blocks.append("\n".join(current))
            return blocks
        except (subprocess.TimeoutExpired, FileNotFoundError):
            # awk not available — fallback to grep
            return self._fallback_query("done")

    def query_by_worker(self, worker: str) -> list[str]:
        """Return step blocks for a specific worker."""
        import subprocess
        try:
            result = subprocess.run(
                ["awk", f'/## STEP:.*worker: {worker}/,/## END/', self.path],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return []
            blocks = []
            current = []
            for line in result.stdout.split("\n"):
                if line.startswith("## STEP:") and current:
                    blocks.append("\n".join(current))
                    current = [line]
                else:
                    current.append(line)
            if current:
                blocks.append("\n".join(current))
            return blocks
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return self._fallback_query(worker, by_worker=True)

    def query_failed(self) -> list[str]:
        """Return failed step lines for status reporting."""
        import subprocess
        try:
            result = subprocess.run(
                ["grep", "status: failed", self.path],
                capture_output=True, text=True, timeout=5,
            )
            return [l.strip() for l in result.stdout.split("\n") if l.strip()]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

    def _fallback_query(self, target: str, by_worker: bool = False) -> list[str]:
        """Python fallback when awk unavailable."""
        try:
            raw = Path(self.path).read_text(encoding="utf-8")
        except OSError:
            return []
        blocks = []
        current = []
        in_block = False
        for line in raw.split("\n"):
            if line.startswith("## STEP:"):
                if current and in_block:
                    blocks.append("\n".join(current))
                current = [line]
                if by_worker:
                    in_block = f"worker: {target}" in line
                else:
                    in_block = f"status: {target}" in line
            elif in_block:
                current.append(line)
        if current and in_block:
            blocks.append("\n".join(current))
        return blocks

    def read_raw(self) -> str:
        """Return full scratchpad content — only for merge/display."""
        try:
            return Path(self.path).read_text(encoding="utf-8")
        except OSError:
            return ""


# ── Plan ──────────────────────────────────────────────────────────


@dataclass
class PlanStep:
    id: str
    worker: str
    task: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Plan:
    steps: list[PlanStep]


def parse_plan(raw: str) -> Plan | None:
    """Parse JSON plan from orchestrator. Returns None on failure (retry/fallback)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict) or "steps" not in data:
        return None

    steps_raw = data["steps"]
    if not isinstance(steps_raw, list) or not steps_raw:
        return None

    steps = []
    for s in steps_raw:
        if not isinstance(s, dict):
            return None
        sid = s.get("id", "")
        worker = s.get("worker", "")
        task = s.get("task", "")
        deps = s.get("depends_on", [])
        if not sid or not worker or not task:
            return None
        if not isinstance(deps, list):
            deps = []
        steps.append(PlanStep(id=sid, worker=worker, task=task, depends_on=deps))

    return Plan(steps=steps)


def get_ready_steps(plan: Plan, completed: set[str], failed: set[str] | None = None) -> list[PlanStep]:
    """Return steps whose dependencies are all satisfied.
    
    Steps that depend on a failed step are skipped (failure isolation).
    """
    failed = failed or set()
    ready = []
    for step in plan.steps:
        if step.id in completed or step.id in failed:
            continue
        if any(dep in failed for dep in step.depends_on):
            failed.add(step.id)
            continue
        if all(dep in completed for dep in step.depends_on):
            ready.append(step)
    return ready


# ── Provider routing ──────────────────────────────────────────────


def get_worker_provider(
    profile_name: str,
    provider_manager: Any,  # ProviderManager
    profile_primaries: dict[str, str] | None = None,
) -> Any:  # BaseProvider
    """Resolve a provider for a worker.

    Strategy: per-profile primary → pool fallback.
    If primary fails, round-robin through other available providers.
    """
    from .providers import resolve_provider

    primaries = profile_primaries or _DEFAULT_PROFILE_PRIMARIES

    # Parse provider/model_id format (Ren fleet: "openrouter/qwen/qwen3-coder:free")
    primary_ref = primaries.get(profile_name)
    provider_name = primary_ref
    model_override = None
    if primary_ref and "/" in primary_ref:
        provider_name, model_override = primary_ref.split("/", 1)

    # Try primary first
    if provider_name:
        try:
            return resolve_provider(
                provider_name,
                model=model_override,
                provider_manager=provider_manager,
            )
        except Exception:
            logger.warning(
                "Worker primary '%s' for profile '%s' failed, trying fallback",
                primary_ref, profile_name,
            )

    # Per-profile fallback
    fallback_ref = _DEFAULT_PROFILE_FALLBACKS.get(profile_name)
    if fallback_ref:
        fb_provider, fb_model = (fallback_ref.split("/", 1) if "/" in fallback_ref else (fallback_ref, None))
        try:
            return resolve_provider(
                fb_provider,
                model=fb_model,
                provider_manager=provider_manager,
            )
        except Exception:
            logger.warning(
                "Worker fallback '%s' for profile '%s' failed, trying pool",
                fallback_ref, profile_name,
            )

    # Pool fallback: try any available provider
    all_entries = provider_manager.all_entries()
    for entry in all_entries:
        try:
            return resolve_provider(entry.name, provider_manager=provider_manager)
        except Exception:
            continue

    # Last resort: raise
    raise RuntimeError(f"No available provider for worker profile '{profile_name}'")


# Default profile → provider mapping (overridable)
# Default profile → provider mapping (overridable)
# Includes aliases for backward compatibility with old profile names
_DEFAULT_PROFILE_PRIMARIES: dict[str, str] = {
    # New 5-profile system
    "architect": "opencode-zen-1/nemotron-3-ultra-free",
    "deep-worker": "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    "code-worker": "opencode-zen-2/big-pickle",
    "verifier": "opencode-zen-3/mimo-v2.5-free",
    "web-reader": "openrouter/google/gemma-4-31b-it:free",
    # Old name aliases
    "researcher": "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    "code-reviewer": "opencode-zen-2/big-pickle",
    "debugger": "opencode-zen-3/mimo-v2.5-free",
    "planner": "opencode-zen-1/nemotron-3-ultra-free",
    "project-manager": "opencode-zen/deepseek-v4-flash-free",
    "test-writer": "opencode-zen-4/north-mini-code-free",
}

# Per-profile fallback (tried before falling through to pool)
_DEFAULT_PROFILE_FALLBACKS: dict[str, str] = {
    "architect": "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    "deep-worker": "opencode-zen-1/nemotron-3-ultra-free",
    "code-worker": "opencode-zen/deepseek-v4-flash-free",
    "verifier": "opencode-zen-4/north-mini-code-free",
}


# ── Classification ────────────────────────────────────────────────


def classify_complexity(
    user_input: str,
    provider: Any,  # BaseProvider
) -> bool:
    """Quick classification: does this request need crew orchestration?

    Makes one cheap API call. Returns True if crew is warranted.
    """
    from .providers import ChatMessage

    prompt = (
        "You are a task classifier. Always respond in English only. Never use Chinese characters. Determine if this request requires multi-step "
        "orchestration with multiple specialized agents (research + code + review).\n\n"
        "Answer ONLY 'yes' or 'no'.\n\n"
        f"Request: {user_input[:500]}"
    )
    try:
        resp = provider.chat(
            messages=[ChatMessage(role="user", content=prompt)],
            max_tokens=10,
        )
        return resp.content.strip().lower().startswith("yes")
    except Exception:
        logger.warning("Complexity classification failed, defaulting to single-agent")
        return False


# ── Decomposition ─────────────────────────────────────────────────


def decompose_task(
    user_input: str,
    provider: Any,  # BaseProvider
) -> Plan | None:
    """Decompose a complex task into a JSON plan. Returns None on failure."""
    from .providers import ChatMessage

    prompt = (
        "You are a task decomposer. Always respond in English only. Never use Chinese characters. Break the following request into discrete steps "
        "with dependencies. Each step needs a worker profile and description.\n\n"
        'Available worker profiles: researcher (web search, info gathering), '
        'deep-worker (coding, analysis), code-reviewer (review, test).\n\n'
        "Output ONLY valid JSON — no markdown, no explanation:\n"
        '{"steps": [\n'
        '  {"id": "step1", "worker": "researcher", "task": "...", "depends_on": []},\n'
        '  {"id": "step2", "worker": "deep-worker", "task": "...", "depends_on": ["step1"]}\n'
        "]}\n\n"
        f"Request: {user_input[:1000]}"
    )
    try:
        resp = provider.chat(
            messages=[ChatMessage(role="user", content=prompt)],
            max_tokens=2000,
        )
        plan = parse_plan(resp.content)
        if plan is None:
            # One retry with stricter prompt
            logger.warning("Plan parse failed, retrying with stricter prompt")
            resp = provider.chat(
                messages=[
                    ChatMessage(role="user", content=prompt),
                    ChatMessage(role="assistant", content=resp.content),
                    ChatMessage(
                        role="user",
                        content="That was not valid JSON. Output ONLY the JSON object — "
                        'no markdown, no backticks, just {"steps": [...]}.',
                    ),
                ],
                max_tokens=2000,
            )
            plan = parse_plan(resp.content)
        return plan
    except Exception:
        logger.exception("Task decomposition failed")
        return None


# ── Execution ─────────────────────────────────────────────────────


async def execute_plan(
    plan: Plan,
    agent: Any,  # AIAgent
    scratchpad: CrewScratchpad,
    provider_manager: Any,  # ProviderManager
) -> None:
    """Execute plan: walk dependency graph, spawn workers, collect results.

    Workers run with persistent sessions (each worker profile has its own
    session in the DB) and per-profile provider keys.

    Workers execute via run_child_task (from agent_profiles.py) — same
    execution path as existing spawn_agent but with persistent memory.
    """
    from .agent_profiles import load_all_profiles, run_child_task
    from .toolsets import ToolRegistry
    from .model_tools import register_builtins
    from .crow_state import CrowState
    from .scratchpad import CrewScratchpadDB
    import concurrent.futures

    profiles = load_all_profiles()
    completed: set[str] = set()
    failed: set[str] = set()
    running: dict[str, concurrent.futures.Future] = {}

    # Connect to SQLite scratchpad for foreman monitoring
    try:
        sql_pad = CrewScratchpadDB(db_path=str(agent._db._path))
    except Exception:
        sql_pad = None

    # Module-level thread pool (reused)
    from .tool_executor import _TOOL_EXECUTOR
    executor = _TOOL_EXECUTOR

    # Shared worker tools (built once)
    worker_tools = ToolRegistry()
    register_builtins(worker_tools)

    def _run_worker(step: PlanStep) -> tuple[str, str]:
        nonlocal sql_pad, worker_tools
        """Execute one worker step. Runs in thread. Returns (step_id, result)."""
        profile = profiles.get(step.worker)
        if not profile:
            return step.id, f"Error: unknown profile '{step.worker}'"

        # Write to SQLite scratchpad for foreman monitoring
        if sql_pad:
            sql_pad.write_task(agent.session_id, step.id, step.worker, "running", step.task[:200])

        # Resolve provider for this worker
        try:
            provider = get_worker_provider(step.worker, provider_manager)
        except Exception as exc:
            return step.id, f"Error: no provider for '{step.worker}': {exc}"

        # Use persistent session: worker:profile_name
        worker_session = f"worker:{step.worker}"
        db_path = os.environ.get("CROW_AGENT_DB_PATH")

        # Append pending status to scratchpad
        scratchpad.append_step(step.id, step.worker, "running", "")

        # ponytail: workers get 256k context, no trimming. max_tokens=4096 enough
        # for deep reasoning models (deepseek-v4-flash-free, big-pickle).
        result = run_child_task(
            profile, step.task, provider, worker_tools,
            session_id=worker_session,
            db_path=db_path,
        )

        # Append result to scratchpad
        scratchpad.append_step(step.id, step.worker, "done", result)
        if sql_pad:
            status = "failed" if result.startswith("Error:") or "[PERMANENT]" in result else "done"
            sql_pad.write_task(agent.session_id, step.id, step.worker, status, result[:200])

        # Inject 1-line summary to orchestrator session
        _inject_worker_summary(step.worker, step.id, result)

        return step.id, result

    try:
        # Walk dependency graph (with failure isolation)
        while len(completed) + len(failed) < len(plan.steps):
            ready = get_ready_steps(plan, completed | set(running.keys()), failed)
            if not ready and not running:
                if failed:
                    logger.warning("Crew: %d completed, %d failed, %d skipped",
                                   len(completed), len(failed),
                                   len(plan.steps) - len(completed) - len(failed))
                else:
                    logger.error("Crew deadlock: no ready steps and none running")
                break

            # Spawn all ready steps in parallel
            for step in ready:
                fut = executor.submit(_run_worker, step)
                running[step.id] = fut

            # Wait for at least one worker to complete
            if running:
                done_futures = concurrent.futures.as_completed(list(running.values()))
                for done_fut in done_futures:
                    step_id, result = done_fut.result()
                    del running[step_id]
                    if result.startswith("Error:") or "[PERMANENT]" in result:
                        failed.add(step_id)
                        scratchpad.append_step(step_id, "?", "failed", result)
                        logger.warning("Crew step '%s' FAILED", step_id)
                    else:
                        completed.add(step_id)
                        logger.info("Crew step '%s' completed (%d/%d)", step_id, len(completed), len(plan.steps))
                    break
    finally:
        pass  # module-level executor, no shutdown needed


# ── Merge ─────────────────────────────────────────────────────────


def build_merge_prompt(scratchpad: CrewScratchpad) -> str:
    """Build synthesis prompt from completed scratchpad steps."""
    done_blocks = scratchpad.query_done()
    if not done_blocks:
        return ""

    combined = "\n\n".join(done_blocks)
    return (
        "You are a report synthesizer. Below are results from multiple specialized "
        "agents who worked on a task. Synthesize them into a single, coherent, "
        "well-structured report. Include all key findings, code, and recommendations. "
        "Be concise but complete.\n\n"
        "--- AGENT RESULTS ---\n\n"
        f"{combined}\n\n"
        "--- END RESULTS ---\n\n"
        "Synthesized report:"
    )


def _inject_worker_summary(worker: str, step_id: str, result: str) -> None:
    """Inject a 1-line summary of worker result into orchestrator session.

    Uses Crow's own session via CROW_AGENT_DB_PATH. Best-effort — never blocks.
    """
    try:
        from .crow_state import CrowState
        db = CrowState()
        # Truncate to 1-2 sentences
        summary = result.strip().split(".")[0][:200] + "."
        db.append_turn("default", "assistant",
                       f"[Worker {worker}] Step '{step_id}': {summary}")
        db.close()
    except Exception as e:
        logger.warning("Worker DB persist failed: %s — result preserved", e)  # ponytail: best-effort, never break crew execution


def merge_results(
    scratchpad: CrewScratchpad,
    provider: Any,  # BaseProvider — main provider (fallback)
    provider_manager: Any | None = None,  # ProviderManager — for free key
) -> str:
    """Synthesize final report from all completed scratchpad steps.

    Uses free Zen key if available via provider_manager, falls back to main provider.
    """
    from .providers import ChatMessage, resolve_provider

    prompt = build_merge_prompt(scratchpad)
    if not prompt:
        return "Crew completed but no results were produced."

    # Try free key first
    merger = provider
    if provider_manager:
        for entry in provider_manager.all_entries():
            if "zen" in entry.name.lower():
                try:
                    merger = resolve_provider(entry.name, provider_manager=provider_manager)
                    logger.info("Merge using free key: %s", entry.name)
                    break
                except Exception:
                    continue

    _status_note = ""
    _failed = scratchpad.query_failed()
    if _failed:
        _status_note = "\n\n## Failed Steps\n" + "\n".join(
            f"- {s}" for s in _failed
        )

    try:
        resp = merger.chat(
            messages=[ChatMessage(role="user", content=prompt)],
            max_tokens=4096,
        )
        return resp.content.strip() + _status_note
    except Exception as exc:
        logger.exception("Merge failed (tried free key first)")
        return "Crew results (merge failed):\n\n" + "\n\n---\n\n".join(
            scratchpad.query_done()
        ) + _status_note
