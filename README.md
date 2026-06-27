# Crow Agent

Your own AI assistant. Runs on your machine, with your keys, on your terms.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/USER/crow-agent/main/install.sh | bash
```

That's it. The script handles Python checks, venv, clone, and install.

When it finishes, `crow` starts the web UI at http://localhost:8000.

**No key?** The first-run setup page lets you paste one. Get a free key at [openrouter.ai/keys](https://openrouter.ai/keys).

## Other ways to install

```bash
# Manual (if you prefer pip)
git clone https://github.com/USER/crow-agent
cd crow-agent
pip install -r requirements.txt   # or: pip install -e .
cp .env.example .env
crow

# Windows (PowerShell)
git clone https://github.com/USER/crow-agent
cd crow-agent
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\crow
```

## What You Need

| Key | Required | Where to get it |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | [openrouter.ai/keys](https://openrouter.ai/keys) — free tier |
| `TELEGRAM_TOKEN` | No | [@BotFather](https://t.me/BotFather) on Telegram |
| `HF_API_KEY` | No | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) — image gen |

## What It Does

- **Chat** — web UI, Telegram, or CLI
- **Background tasks** — delegate work, get results later
- **Cron jobs** — scheduled reports, backups, checks
- **Memory** — teaches itself from conversations. Full-text search.
- **Tools** — web search, file ops, git, image gen, speech, SSH, and more

## Interfaces

| Interface | Command | Details |
|---|---|---|
| Web UI | `crow` | http://localhost:8000 — chat, sessions, cron, dashboard |
| CLI | `crow-agent` | Terminal chat |
| Telegram | Set `TELEGRAM_TOKEN` in `.env` | Chat from your phone |

## Architecture

Crow is a single Python process. Flat architecture — no layers, no microservices.

Every message goes through: **RECALL → ASSEMBLE → LLM → TOOLS → RESPOND**

- **RECALL** — FTS5 full-text search on conversation history
- **ASSEMBLE** — context assembly with token budget (35K cap)
- **TOOL LOOP** — executes tools, internal monologue up to 12 rounds
- **Failover** — primary → secondary LLM provider chain

## Project Structure

```
├── app.py                  # FastAPI web server
├── crow_agent/             # Core agent
│   ├── run_agent.py        #   State machine
│   ├── providers.py        #   LLM provider + failover
│   ├── crow_state.py       #   SQLite + FTS5 memory
│   ├── cron_engine.py      #   Scheduled jobs
│   ├── tools_*.py          #   Tool modules
│   └── ...
├── templates/              # Jinja2 HTML
├── tests/                  # pytest suite
├── skills/                 # Reusable workflows
├── team/                   # Agent profiles
├── extensions/             # Optional plugins
└── docs/                   # Architecture decisions
```

## License

MIT
