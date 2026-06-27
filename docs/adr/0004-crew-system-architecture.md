# Crew System — Multi-Agent Collaboration Architecture

Crow needed multi-agent orchestration with persistent worker memory and token-cost
distribution across multiple free API keys. The existing `spawn_agent`/`spawn_team`
were fire-and-forget — no dependency graph, no shared workspace, no worker memory.

We designed a crew system where Crow (the orchestrator) auto-detects complex tasks,
decomposes them into a JSON dependency graph, spawns workers with persistent sessions
and per-profile provider keys, coordinates via an append-only markdown scratchpad,
and merges results with a final synthesis call.

**Status:** accepted

## What we decided

| Decision | Choice | Why |
|----------|--------|-----|
| Orchestrator | Main Crow, same session | No new entity; `run_agent.py` handles crew as smarter delegation |
| Worker memory | Own SQLite session + inject summary to orchestrator | Persistent self-memory + user recall via FTS5 |
| Scratchpad format | Markdown with `## STEP:`/`## END` delimiters + `status: done` tag | LLMs write markdown flawlessly; scripts query precisely |
| Plan format | JSON with `{id, worker, task, depends_on}` | Single structured output, retry on parse failure, sequential fallback |
| Crew activation | Keyword-trigger (task words like "build", "research") | Zero API calls; fast skip for trivial requests |
| Merge strategy | Free Zen key synthesis (pool fallback to main) | Main key not charged for merge |
| Worker tools | Profile-defined only (no spawn) | Role-scoped: researcher gets web_search, coder gets write_file |
| Provider routing | Per-profile primary + shared pool fallback | Predictable quality; cost spreading on primary failure |
| Concurrency | Parallel within dependency level | Append-only scratchpad (`>>`) avoids locks |

## Considered options

- **Persistent team with identities (Crew roster)** — rejected: user wanted stateless
  workers upgraded to stateful, not new persistent entity types.
- **JSONL scratchpad** — rejected: LLMs produce markdown more reliably than JSON.
- **Free-text plan, code-parsed** — rejected: plan is single structured output, not
  multi-worker freeform; JSON parse + retry + fallback is robust.
- **Full tool access for all workers** — rejected: researcher with `write_file` is a
  risk with no upside. Profile-scoped tools match the worker's role.
- **Orchestrator reads scratchpad directly (no merge call)** — rejected: synthesis
  call is cheap and produces higher-quality final reports.

## Consequences

- **Classifier simplified (2026-06-18)**: keyword-trigger replaces LLM classification call.
  Saves 1 API call per turn on the main key.
- **Merge uses free Zen key** (2026-06-18): synthesis call routes to a free Zen provider.
  Main provider only pays for decomposition (1 call per crew run).
- **v1 complete (2026-06-18)**: all loose ends closed.
  - Worker summary injection: each worker injects 1-line summary to orchestrator session.
  - Profile tool filtering: workers only get tools listed in their profile.
  - Scratchpad TTL: cleanup files older than 1 hour.
  - Worker session TTL: prune sessions older than 7 days via CrowState.prune_worker_sessions().
- Per-profile provider primaries + pool means pool keys may stay idle if primaries
  never fail. Trade-off: predictability over max cost spreading.