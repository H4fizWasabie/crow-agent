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

## What It Does

- **💬 Chat** — web UI, Telegram, or terminal
- **⚡ Background tasks** — "Crow, research X and report back"
- **⏰ Cron jobs** — scheduled reports, backups, recurring checks
- **🧠 Memory** — learns from conversations, full-text search via FTS5
- **🔧 Tools** — web search, file ops, git, image gen, speech-to-text, SSH, MCP, LSP, and more
- **🔄 Failover** — if one LLM provider goes down, it tries another

## Interfaces

| Interface | Start | Where |
|---|---|---|
| Web UI | `crow` | http://localhost:8000 |
| CLI | `crow-agent` | Terminal |
| Telegram | `TELEGRAM_TOKEN` in `.env` | Your phone |

## How It Works

Every message runs through a state machine:

```
RECALL → ASSEMBLE → LLM → TOOLS → RESPOND
```

- **RECALL** — FTS5 search across conversation history + memory vault
- **ASSEMBLE** — tiered context budget, 35K token cap
- **TOOL LOOP** — agent executes tools autonomously, up to 12 rounds
- **RESPOND** — saves to DB, updates memory, auto-verifies code changes

The agent uses **internal monologue** — text without tool calls is treated as thinking, not output. Crow keeps working until it has a real answer.

## Why Crow?

- **Single-user by design** — no auth, no workspaces, no billing pages
- **BYO keys** — no subscription, no vendor lock
- **Cost-aware** — cheap models for simple tasks, smarter ones for hard problems
- **Follows through** — delegates background tasks and delivers results
- **Flat architecture** — one Python process, no microservices, no layers

## Project Structure

```
├── app.py                  # FastAPI web server + SSE streaming
├── install.sh              # One-line installer
├── crow_agent/             # Core agent
│   ├── run_agent.py        #   State machine orchestrator
│   ├── providers.py        #   LLM provider abstraction + failover
│   ├── crow_state.py       #   SQLite + FTS5 memory
│   ├── cron_engine.py      #   Scheduled job runner
│   ├── heartbeat_engine.py #   Autonomous background loop
│   ├── crew.py             #   Multi-agent orchestration
│   ├── tools_*.py          #   Tool modules
│   └── ...
├── templates/              # Jinja2 HTML
├── tests/                  # pytest suite
├── skills/                 # Reusable agent workflows
├── team/                   # Specialized agent profiles
├── extensions/             # Optional plugins
└── docs/adr/               # Architecture Decision Records
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — do what you want. If it breaks, you get to keep both pieces.
