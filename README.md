# Crow Agent

Your own AI assistant. Runs on your machine, with your keys, on your terms.

Crow chats with you on Telegram, in your browser, or in the terminal. It
remembers things, does background tasks, fetches news, nags you about
deadlines, and generally tries to be useful. Sometimes it even succeeds.

## Quick Start

### For New Users (step by step)

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/crow-agent
cd crow-agent

# 2. Install
pip install -e .

# 3. Configure
cp .env.example .env

# 4. Edit .env — add at least one LLM provider API key
nano .env

# 5. Run
crow          # web UI at http://localhost:8000
# or:
crow-agent    # chat in the terminal
```

**That's it.** If something doesn't work, the startup check will tell you what's missing.

### What Next?

- Chat with Crow in the web UI, Telegram, or terminal
- Try: "what's the latest AI news?" or "search for X"
- Delegate a background task: "look into Y and report back"
- Set up cron jobs in the web UI at `/cron`
- Configure Telegram: add `TELEGRAM_TOKEN` to `.env`
- Check the `/dashboard` for usage stats

## API Keys

Crow uses different API keys for different features. You only need what you use.

| Key | Required | What it's for |
|---|---|---|
| `OPENCODE_GO_API_KEY` | Yes | Default LLM (free tier at opencode.ai) |
| `OPENROUTER_API_KEY` | No | Fallback LLM (openrouter.ai — free models available) |
| `TAVILY_API_KEY` | No | Job search (daily jobs report) |
| `HF_API_KEY` | No | Image generation (free: huggingface.co/settings/tokens) |
| `TELEGRAM_TOKEN` | No | Telegram bot |
| `THREADS_ACCESS_TOKEN` | No | Social media posting |

**Custom LLM providers:** Set any `{NAME}_API_KEY`, `{NAME}_BASE_URL`, and `{NAME}_MODEL` — Crow auto-detects them on startup.

**Recommended starter setup:**
1. `OPENCODE_GO_API_KEY` — free, works out of the box
2. `OPENROUTER_API_KEY` — free models, acts as fallback if primary is down
3. `TAVILY_API_KEY` — needed for the daily jobs report

## What It Does

- **Chat** — Telegram, web UI, or CLI. Pick your poison.
- **Background tasks** — "Crow, look into this and report back." It will.
- **Cron jobs** — daily news digests, backups, wiki linting. Set and forget.
- **Reminders** — deadline nagging with snooze. Yes, it will keep bugging you.
- **Code+doc knowledge** — understands a codebase or document set via graphify.
- **Memory** — remembers what you teach it. Vault with full-text search.
- **Tools** — web search, file ops, git, SSH, image gen, Telegram, and more.
- **Failover** — if one LLM provider goes down, it tries another.

## Architecture

Crow is a single Python process serving multiple interfaces.

```
┌─ Telegram Bot ─┐  ┌─ Web UI (FastAPI/SSE) ─┐  ┌─ CLI ─┐
└───────┬────────┘  └───────────┬──────────────┘  └───┬───┘
        └──────────┬────────────┴─────────┬───────────┘
                   │                      │
              ┌────▼────┐           ┌─────▼──────┐
              │ AIAgent │◄──────────│  CronEngine │
              │ (state  │    runs   │  ReminderEng│
              │  machine│    jobs   │  (async bg) │
              └──┬──┬───┘           └────────────┘
         ┌───────┘  └───────┐
    ┌────▼────┐        ┌────▼──────┐
    │  Tools  │        │  Memory   │
    │ (50+    │        │  Vault    │
    │  tools) │        │  (FTS5)   │
    └────┬────┘        └───────────┘
         │
    ┌────▼─────────────────────────┐
    │  LLM Providers (failover)    │
    │  OpenCode Go → OpenRouter →  │
    │  Custom providers            │
    └──────────────────────────────┘
```

### Stack

| Layer | What |
|---|---|
| Runtime | Python 3.12+, asyncio |
| Web | FastAPI + uvicorn + Jinja2 |
| DB | SQLite (WAL mode, FTS5, 5s busy timeout) |
| Chat | SSE streaming (web) / python-telegram-bot (Telegram) |
| LLM | Custom httpx-based providers with failover chain |
| Scheduler | In-process async CronEngine + ReminderEngine |

### Turn Lifecycle

Every user message goes through this state machine:

```
IDLE → RECALL → ASSEMBLE → CALL → [TOOL_LOOP] → RESPOND → IDLE
```

1. **RECALL** — FTS5 search on conversation history + vault keyword match + semantic search
2. **ASSEMBLE** — Three-tier context budget: required → high priority → low priority (35K token cap)
3. **CALL** — LLM provider call with tool schemas
4. **TOOL_LOOP** — Execute tool calls, internal monologue (LLM text without tools = context, not response), coaching signals at rounds 4/8, hard ceiling at round 12, 120s cap
5. **RESPOND** — Save to DB, update memory tracker, auto-verify modified code

### Key Design Choices

- **Internal monologue** — LLM text responses without tool calls become context, not user-facing output. Crow keeps working uninterrupted instead of prematurely responding. Text is thinking, not talking.
- **Initiative system** — Heartbeat detects problems (test failures, cron failures, pending delegates), spawns autonomous agent turns. Crow runs itself and reports via Crow Log channel.
- **Synchronous agent loop** — AIAgent runs sync; web, Telegram, and cron all wrap it in a thread pool. No async tools, no concurrency bugs.
- **Single SQLite DB** — WAL mode for concurrent reads, retry for write contention. Good enough for one user.
- **Failover LLM** — Primary → secondary → tertiary. 9 provider keys. Each has its own key and model config.
- **Output compression** — Tool outputs over 500 chars get stored with a retrieval ID. Agent calls `retrieve(id)` for details.
- **Auto-verification** — Code changes checked after TOOL_LOOP. Catches hallucinations before they land in your repo.
- **English-only enforcement** — All agents, crew, and team profiles require English output.

## Why Another AI Assistant?

Because none worked the way I wanted. Crow is:

- **Single-user by design** — no auth ceremony, no workspaces, no billing page.
- **BYO keys** — no subscription, no vendor lock. Bring your own LLM tokens.
- **Cost-aware** — uses cheap models for simple work, delegates hard stuff to smarter ones.
- **Follows through** — actually delivers results instead of just saying "done."

## Environment Variables

See [.env.example](.env.example) for the full list.

| Variable | Required | Description |
|---|---|---|
| `OPENCODE_GO_API_KEY` | Yes | Default LLM provider |
| `TELEGRAM_TOKEN` | No | Telegram bot token |
| `TELEGRAM_ALLOWED_IDS` | No | Comma-separated Telegram user IDs |
| `CROW_WEB_TOKEN` | No | Bearer token for web UI auth |
| `CROW_VPS` | No | SSH target for deployment |

## Project Structure

```
├── app.py                  # FastAPI web server + SSE streaming
├── crow_agent/             # Core agent
│   ├── run_agent.py        #   State machine orchestrator
│   ├── providers.py        #   LLM provider abstraction + failover
│   ├── cron_engine.py      #   Scheduled job runner
│   ├── reminder_engine.py  #   Deadline task nagger
│   ├── task_registry.py    #   Background task queue
│   ├── crow_state.py       #   SQLite session store + FTS5
│   ├── memory_tracker.py   #   Preference + skill extraction
│   ├── tools_*.py          #   Tool modules (web, file, git, etc.)
│   └── ...
├── scripts/                # Utility scripts (reports, deploy, journal)
├── templates/              # Jinja2 HTML templates
├── tests/                  # pytest suite
├── skills/                 # Reusable agent skill definitions
├── team/                   # Specialized agent profiles
├── docs/adr/               # Architecture Decision Records
├── memory vault/           # Knowledge wiki (Obsidian vault)
├── CONTEXT.md              # Domain glossary
├── .env.example            # Environment variable reference
└── deploy/                 # Systemd unit + config
```

## License

MIT. Do what you want. If it breaks, you get to keep both pieces.
