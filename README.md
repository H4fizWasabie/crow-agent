# 🐦‍⬛ Crow Agent

<p align="center">
  <strong>Your own AI assistant. Runs on your machine, with your keys, on your terms.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
</p>

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/H4fizWasabie/crow-agent/main/install.sh | bash
```

One command. The script handles Python, venv, clone, and install. When it finishes, `crow` opens the web UI at `http://localhost:8000`.

**No API key?** The first-run setup page asks for one. Get a free key at [openrouter.ai/keys](https://openrouter.ai/keys).

### Manual install

```bash
git clone https://github.com/H4fizWasabie/crow-agent
cd crow-agent
pip install -r requirements.txt
cp .env.example .env
crow
```

### Windows

```powershell
git clone https://github.com/H4fizWasabie/crow-agent
cd crow-agent
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\crow
```

## What You Need

| Key | Required | Get it here |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | [openrouter.ai/keys](https://openrouter.ai/keys) — free tier |
| `TELEGRAM_TOKEN` | Optional | [@BotFather](https://t.me/BotFather) — Telegram bot |
| `HF_API_KEY` | Optional | [huggingface.co](https://huggingface.co/settings/tokens) — image generation |

Any OpenAI-compatible API works. Set `{NAME}_API_KEY`, `{NAME}_BASE_URL`, `{NAME}_MODEL` — Crow auto-detects them.

## Features

| Category | What it does |
|----------|-------------|
| **💬 Chat** | Web UI, Telegram, or terminal — full streaming, tool calls, memory |
| **🎯 Goals** | Self-directed objectives — Crow creates, tracks, and persists goals across sessions |
| **🧠 Self-Journal** | Mood tracking, reflection, and lesson learning after every turn |
| **🔍 Sensors** | Background system monitoring — CPU, RAM, disk, file changes — injected as context |
| **⚡ Background tasks** | "Crow, research X and report back" — delegated to specialist agents |
| **⏰ Cron jobs** | Scheduled reports, backups, recurring checks |
| **📋 Crew system** | Multi-agent orchestration — decompose, delegate, merge results |
| **🚑 Crash recovery** | Checkpoint system — every 3 rounds saved, auto-resume on restart |
| **🛡️ Failover** | Provider chains — when one LLM fails, next takes over transparently |
| **🔧 31 Tools** | read/write/edit, run_cmd, web search/crawl, media, SSH, cron, MCP, more |
| **🔄 Update checker** | Auto-notifies when a new version is available on startup |

## What's New

- **Goals system** — Crow creates and tracks self-directed objectives. Goals survive restarts and are injected into every turn's context. Progress updates automatically after each turn.
- **Self-journal** — After each turn, Crow reflects on what it did (mood, reflection, lesson). Stored in SQLite, injected as `## Self` context.
- **Background sensor** — Monitors CPU, RAM, disk usage, and file changes in watched directories. Injected as `## Surroundings` context so Crow sees system state.
- **Checkpoint crash recovery** — Every 3 tool rounds, state is saved to `~/.crow_agent/active_tasks/`. If Crow crashes mid-task, it resumes automatically on next startup.
- **Team profiles** — 6 specialized agent profiles (architect, code-worker, deep-worker, verifier, web-reader, heartbeat) with per-profile provider fallback chains.
- **Honesty check** — If Crow claims verification ("I checked the file") without using read tools, it appends a warning.

## Interfaces

| Interface | Start | Where |
|---|---|---|
| Web UI | `crow` | http://localhost:8000 |
| CLI | `crow-agent` | Terminal |
| Telegram | `TELEGRAM_TOKEN` in `.env` | Your phone |

## How It Works

Every message runs through a state machine:

```
RECALL → ASSEMBLE → CALL → TOOL LOOP → RESPOND
```

- **RECALL** — FTS5 search across conversation history + memory vault + semantic embeddings
- **ASSEMBLE** — Tiered context budget (120K tokens). Injects goals, self-awareness, surroundings, matched skills, budget notice
- **CALL** — Initial LLM call with all context. Internal monologue (text without tools = thinking)
- **TOOL LOOP** — Up to 999 rounds. Parallel batching suggested at round 2. Checkpoint saved every 3 rounds
- **RESPOND** — Saves to DB, updates goal progress, self-reflection, skill extraction, session state save

The agent never narrates intent — if the next step is obvious, it executes it immediately.

### Crew Orchestration

For complex tasks (multiple files, building features, debugging), Crow decomposes the work:

1. **Classify** — detect if task needs multiple specialists
2. **Decompose** — break into dependency-ordered steps with worker profiles
3. **Execute** — run workers in parallel via thread pool, each with its own provider and toolset
4. **Merge** — synthesize results into a coherent report

Workers log progress to an SQLite scratchpad monitored by the **Foreman** — embedding drift detection catches stalled workers.

### Autonomous Heartbeat

Crow runs a background loop every 10 minutes that:

- **Observes** — git changes, task deadlines, cron failures, system health
- **Decides** — uses a cheap LLM to classify what needs attention
- **Acts** — spawns initiative turns for specialist agents (code-worker, debugger, researcher, etc.)
- **Self-manages** — tracks its own mood, learns from mistakes, abandons stale goals

## Project Structure

```
├── app.py                  # FastAPI web server + SSE streaming
├── install.sh              # One-line installer
├── crow_agent/             # Core agent
│   ├── run_agent.py        #   State machine orchestrator
│   ├── providers.py        #   LLM provider abstraction + failover
│   ├── crow_state.py       #   SQLite + FTS5 memory + goals + journal
│   ├── sensors.py          #   Background system monitoring
│   ├── heartbeat_engine.py #   Autonomous background loop
│   ├── crew.py             #   Multi-agent orchestration
│   ├── foreman.py          #   Crew task monitoring + stall detection
│   ├── scratchpad.py       #   SQLite crew task tracker
│   ├── error_tracker.py    #   Recurring error tracking with escalation
│   ├── update_checker.py   #   Auto-update notification
│   ├── tools_*.py          #   Tool modules
│   └── ...
├── templates/              # Jinja2 HTML
├── tests/                  # pytest suite (200+ tests)
├── skills/                 # Reusable agent workflows
├── team/                   # 6 specialized agent profiles
├── extensions/             # Optional plugins (crawl4ai, etc.)
└── docs/adr/               # Architecture Decision Records
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — do what you want. If it breaks, you get to keep both pieces.
