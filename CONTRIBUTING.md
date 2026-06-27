# Contributing to Crow

Thanks for helping make Crow better.

## Setup

```bash
git clone https://github.com/your-fork/crow-agent
cd crow-agent
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env  # add your API key
```

## Running

```bash
.venv/bin/python app.py    # web UI at http://localhost:8000
```

## Testing

```bash
.venv/bin/python -m pytest tests/ -v
```

288+ tests. PRs must pass all tests.

## Architecture

Crow is a flat single-folder agent. No layers, no closures-as-hooks, no LoopConfig.

```
crow_agent/
  run_agent.py         # Core agent loop (AIAgent)
  context_assembler.py # RECALL + ASSEMBLE pipeline
  prompt_builder.py    # Token budget + truncation
  crow_state.py        # SQLite session store with FTS5
  self_model.py        # Mood, health, awareness
  crew.py              # Multi-agent crew orchestration
  heartbeat_engine.py  # Autonomous background loop
  memory_tracker.py    # Preference + skill extraction
  tools_*.py           # Individual tool modules
  providers.py         # LLM provider abstraction
```

See DEV_GUIDE.md for detailed architecture.

## Adding tools

Tools are registered via decorators in `tools_*.py`:

```python
@registry.register(description="My tool description")
def my_tool(param: str) -> str:
    return f"Result: {param}"
```

Register the module in `model_tools.py > register_builtins()`.

## Code style

- Flat architecture — direct function calls, no callback chains
- `ponytail:` comments mark best-effort code (never block on failure)
- Types encouraged but not mandatory
- English-only for code and comments
