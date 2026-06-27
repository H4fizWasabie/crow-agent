# ADR 0009: Crow 2.0 — Port Ren Concepts Into Crow's Flat Architecture

> **Target folder:** Copy this file to your project's `docs/adr/` directory
> **Status:** Proposed — 2026-06-27

## Context

Ren was built as a layered evolution of Crow (ren_ai → ren_agent → ren_coding_agent). That 3-layer architecture gave Ren 22 mechanisms, 14-agent fleet, self-model awareness, and 400 tests — but at a cost: ~30 invisible disconnection points found in a single day of debugging (steering `continue` missing, ANTICIPATE hook dead, 7 heartbeat slices unwired, reply context wiped).

**Crow's advantage:** Everything is visible. The agent loop is ~1,900 lines across `run_agent.py` + `telegram_bot.py`. No LoopConfig abstraction, no EventBus, no separate agent state machine. Every connection is a single grep away. 0 invisible disconnection points.

## Decision

**Port Ren's key concepts into Crow's flat architecture. Keep Crow's design philosophy. Delete Ren's layering.**

We port the WHAT, not the HOW.

### Loop Engineering Philosophy

Ren's conceptual design was correct: turn lifecycle awareness, self-model introspection, fleet orchestration, self-healing heartbeat. The ideas are sound.

The mistake was in the **implementation pattern:** 3-layer architecture with closures as hooks passed through LoopConfig → EventBus → ResourceLoader → Agent. Every concept crossed 3-5 file boundaries. A single missing `continue` statement on line 314 of `agent_loop.py` silently broke narration detection for weeks. The `get_follow_up_messages` closure was created but never assigned — invisible until we audited the code.

**Crow 2.0's rule: every mechanism is a direct function call in the same file.** The tool loop in `run_agent.py` has narration detection, read-lock, and budget ceiling as inline checks — no callbacks, no closures, no LoopConfig. If something's not firing, you grep for the function name and find it immediately.

```
AIAgent.run_stream()
  while tool_calls:
    → LLM call
    → tool execution (inline try/except per tool)
    → narration_check (inline regex — "i will", "saya akan")
    → read_lock_check (counter, force-stop at 3)
    → budget_ceiling (inline counter, 12 rounds)
  → post_turn: self_model.push_health()      (direct call)
  → post_turn: PreferenceSniffer.sniff()     (direct call)
  → post_turn: context_summary → self_model  (direct call)
```

Ren's power, Crow's simplicity. That's the fork.

| Concept | Where it lives in Crow | How it ports |
|---|---|---|
| Self-model (mood, health, awareness) | New `crow_agent/self_model.py` | SQLite blob — simple, flat. Push updates inline after turn. No callback hook system. |
| Turn lifecycle (narration detection, read-lock, budget ceiling) | Inline in `run_agent.py` tool loop | Regex checks + counters inside `while tool_calls:` — no `should_stop_after_turn`/`prepare_next_turn` callbacks |
| Per-turn context reconstruction | `context_assembler.py` | Already exists in Crow. Add FTS5 weighting + LLM summarization (from Ren) |
| Crew A2A (decompose→execute→merge) | `crew.py` | Already ported from Crow to Ren today (ADR 0008). Adapt paths for Crow. |
| 7 heartbeat slices (health, rescue, code check, self-edit, eval deploy, weekly sweep, notify) | `heartbeat_engine.py` | Wire into `_loop()` — all 7 slices now active (copy loop from Ren) |
| Tool result persistence | `telegram_bot.py` | Add `_summarize_tool_results()` inline in the response handler |
| Post-loop narration synthesis | `telegram_bot.py` | Detect narration patterns in final response → re-synthesize from tool context |
| Warm fallback text | `telegram_bot.py` | Replace robotic "I processed your request but couldn't produce a response" |
| max_turns enforcement | `run_agent.py` | Already has ceiling — verify it's enforced |
| Web search fallback (DDG → Wikipedia) | `tools_web.py` | Copy the Wikipedia API fallback from Ren |
| Malay narration patterns | `run_agent.py` | Add to narration detection regex list |
| WAL auto-checkpoint + mmap | `crow_state.py` | Copy pragmas from Ren (`wal_autocheckpoint=128`, `mmap_size=256MB`) |
| Thinking stream separation | `telegram_bot.py` | Separate log channel message for thinking |
| Turn threshold evaluation | `memory_tracker.py` | Fire LLM self-eval at 10/50/100/500 turns |

## What NOT To Port

These are Ren's architectural choices that caused the disconnection problems:

- **3-layer architecture** (ren_ai/ren_agent/ren_coding_agent)
- **LoopConfig** dataclass — 16 fields, 9 hook callbacks
- **EventBus** pub/sub system
- **ResourceLoader** — lazy-loading 10 resources, only 3 actually used
- **Agent.protocol `run()` vs `prompt()` dual path** — pick one
- **`_run_with_agent` / `_run_with_worker` / `_run_with_crew` triple** — one entry point: `agent.prompt()`
- **Turn hooks as closures** — inline code instead: regex + counters in the loop
- **Session.jsonl** — CrowState SQLite is sufficient
- **PlanSavepoint** — git stash is simpler
- **SacredCore** — git reset --hard is simpler
- **Extension system** — tools modules are sufficient
- **Eval harness** — pytest is sufficient
- **Cron engine** — already in Crow, works
- **Prompt templates** — dead code, never used

## Crow 2.0 Architecture

Crow stays **one folder, flat modules, no layers:**

```
crow_agent/
  run_agent.py            ← ~1,253 lines. Core agent loop (AIAgent class)
  telegram_bot.py         ← ~654 lines. PTB bot, response handler
  context_assembler.py    ← RECALL + ASSEMBLE pipeline
  context_sources.py      ← Pluggable recall sources
  prompt_builder.py       ← Token budget + truncation
  crow_state.py           ← SQLite/FTS5 episodic memory
  self_model.py           ← NEW: mood, health, awareness (SQLite blob)
  crew.py                 ← Unified registry + decompose→execute→merge
  heartbeat_engine.py     ← 7 slices wired + rescue + health
  memory_tracker.py       ← Preference sniffing + sequence extraction
  turn_finalizer.py       ← Post-turn cleanup
  toolsets.py             ← Tool registry
  providers.py            ← LLM providers
  telegram_rich.py        ← HTML formatting (Crow parity)
  ...
```

## Implementation Phases (13-16 hours estimated)

### Phase 1: Self-Model (3-4 hours)
- Create `crow_agent/self_model.py` — SQLite-backed mood/health/awareness
- `SelfModel` class: `update(path, value)`, `snapshot()`, `mood()`, `to_prompt_chunk()`
- Push updates inline in `run_agent.py` after each turn completes: health stats, turn count, conversation summary
- Inject `to_prompt_chunk()` into system prompt via `context_assembler.py`
- Mood engine: sharp/normal/degraded from health + reflection + initiative success
- `can_act()` soft-block: disk >95%, RAM >95%, error streak ≥ 5

### Phase 2: Inline Turn Lifecycle (2-3 hours)
- Move narration detection regex (EN: "i will", "let me", "i need to" / MS: "saya akan", "jom saya", "biar saya") inline in `run_agent.py` tool loop
- Move read-lock counter inline: `_read_streak`, force-stop at ≥ 3
- Move budget ceiling check inline: `_LOOP_HARD_CEILING = 12`, inject exhaustion prompt, one final synthesis call
- Verify max_turns enforcement (Crow's existing ceiling)
- Steering injection: when narration detected, inject system message and re-call LLM — no callback, just inline code

### Phase 3: Heartbeat & Self-Healing (2-3 hours)
- Copy `_loop()` from Ren's `heartbeat_engine.py` — wire all 7 slices
- Slices: pre_check, module_fix, daily_agenda, advance_agenda, deep_scan, reflect, decide, health_check, code_check, rescue, self_edit, eval_deploy, weekly_sweep, compact_user_model, notify, hourly_report
- Gating: `_slice_is_enabled()` with env var `HEARTBEAT_ENABLE_SLICES`
- Copy health check helpers: `_scan_turns_for_errors`, `_scan_log_for_errors`, `_check_critical_scripts`
- Wire `_hb_send` dual delivery (log channel + user DM)

### Phase 4: Context Pipeline (2 hours)
- Add FTS5 recency weighting to `crow_state.search()`: `bm25() * (1 + days_old * 0.05)`
- Add LLM summarization on truncation: `summarize_dropped_turns()` calls cheap model
- Add source-level token logging to `context_assembler.py`
- Add USER_MODEL.md compaction to heartbeat (`_slice_compact_user_model`)

### Phase 5: UX Polish (1-2 hours)
- Fix web search fallback (DDG → Wikipedia API) in `tools_web.py`
- Add anomaly-modal detection for DDG blocking
- Add post-loop narration synthesis in `telegram_bot.py`
- Warm fallback text: "User, I ran into a wall on that one..."
- Thinking stream → separate log channel reply message

### Phase 6: Turn Threshold + Evolution (1 hour)
- Add turn milestone evaluation (10/50/100/500) in `memory_tracker.py`
- `_evaluate_milestone()`: cheap LLM self-evaluation, append to USER_MODEL.md
- Fire-and-forget via daemon thread

### Phase 7: Tests (2 hours)
- Unit tests for each new module (TDD pattern from Ren)
- Self-model: mood computation, can_act guard, prompt chunk format
- Health helpers: scan turns for errors, scan logs, check scripts
- Rescue: get_rescuable finds stuck/waiting/failed initiatives
- Crew: decompose_task, parse_plan, execute_plan integration

## Shared Modules (Crow ↔ Ren)

**Current state:** Both codebases are entirely separate. Crow has no imports from Ren.

**After port:** Some modules could be shared or symlinked:
- `crow_state.py` — identical schemas, different DB paths
- `prompt_builder.py` — identical logic, different import paths
- `context_sources.py` — identical registries
- `crew.py` — unified worker profiles, same decompose/execute/merge

**Option:** Keep them separate (avoid cross-dependency) or unify them under one shared module path. Given the lesson of the day (disconnection points), I recommend keeping them separate but functionally identical.

## Risks

- **Porting takes 13-16 hours** — Ren works now. Don't break what works.
- **Crow 2.0 needs testing** — every ported concept needs TDD just like we did for Ren.
- **Two codebases to maintain** — unless we decide Crow 2.0 replaces Ren entirely.
- **Crow's provider layer is simpler** — no FailoverProvider, no reasoning injection. Add these carefully.

## References

- Ren ADR 0008: Unified A2A Architecture (crew pipeline)
- Crow ADR 0004: Crew System Architecture (original, superseded by 0008)
- Ren ADR 0007: Goal Manager Replaces Goal Tracker
- Ren AGI_DESIGN.md: 22 mechanisms across 4 dimensions
