# Crow Agent — Developer Guide

Architecture and workflow for developers working on the Crow codebase.

## Architecture

```
crow_agent/
  run_agent.py           # Core agent loop (AIAgent class, state machine)
  telegram_bot.py        # Telegram bot integration
  context_assembler.py   # RECALL + ASSEMBLE pipeline
  context_sources.py     # Pluggable recall sources
  prompt_builder.py      # Token budget + truncation
  crow_state.py          # SQLite/FTS5 episodic memory
  self_model.py          # Mood, health, awareness (SQLite blob)
  crew.py                # Multi-agent crew: decompose → execute → merge
  heartbeat_engine.py    # Autonomous background loop
  memory_tracker.py      # Preference sniffing + skill extraction
  turn_finalizer.py      # Post-turn cleanup
  toolsets.py            # Tool registry (decorator-based)
  providers.py           # LLM provider abstraction
  tools_*.py             # Individual tool modules
  file_safety.py         # Backup, rollback, compile-verify
  failure_classifier.py  # Strategy-level failure categories
```

## State Machine

```
IDLE → RECALL → ASSEMBLE → CALL → [TOOL_LOOP] → RESPOND → IDLE
```

## Design Philosophy

- **Flat architecture:** Every mechanism is a direct function call in the same file.
- **No closures as hooks:** No LoopConfig, no callback chains. Inline checks.
- **Ponytail pattern:** Best-effort code marked `ponytail:` — never block on failure.
- **Tools via decorators:** `@registry.register(description="...")` — auto schema from type hints.

## Provider Architecture

- OpenAI-compatible API (any provider with `/v1/chat/completions`)
- Multi-provider pool with failover
- Reasoning injection for deep-thinking models
- Embeddings via OpenRouter

## Testing

```bash
.venv/bin/python -m pytest tests/ -v
```

288+ tests. Use `-k "not phase5_alerts"` to skip slow integration tests.

## Key Rules

- English-only enforced across all agents, crew, and team profiles.
- Context budget: 35K tokens default, configurable via `CROW_CONTEXT_BUDGET`.
- `[DONE]` / `[CONTINUE]` tokens signal task completion/continuation.
- Tools gated by `check_fn` — unconfigured tools skipped at startup (no runtime errors).
