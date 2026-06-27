# Async background task execution

Crow's autonomous loop runs sub-agents outside the main Telegram turn. The chosen mechanism is `asyncio.create_task` on the Telegram bot's event loop, not a thread pool or separate process.

## Context

Crow receives a task from the user. The turn has a 300s Telegram wrapper. Deep work (OCR a 50-page PDF, research a domain, write multi-file code) routinely exceeds 300s. The user should get an acknowledgement in <30s and the result later, not wait inline for the full execution.

## Options considered

### Option A: ThreadPoolExecutor

Run `run_child_task` in a thread pool. The main asyncio loop stays responsive. The thread does synchronous HTTP, subprocess, and file I/O.

Tradeoffs:
- Thread safety: SQLite needs locks, shared state needs serialization
- No cancellation mechanism — `thread.cancel()` doesn't exist
- Thread pool sizing adds complexity (is CPU-bound work mixed with IO?)
- Crash in thread kills the process (same as asyncio)

### Option B: asyncio.create_task (chosen)

Create a background coroutine on the Telegram bot's event loop. Sub-agent tools (httpx, subprocess, file I/O) are IO-bound and naturally yield to the event loop during waits. Sync-only tools (file parsing) wrap in `run_in_executor` for the brief CPU portion.

Tradeoffs:
- Sync httpx calls block the event loop briefly (up to 120s per LLM call). During those 120s, the Telegram bot cannot respond to the user. Risk: user sends "stop task X" during a blocking LLM call and Crow doesn't see it until the call finishes.
- No crash isolation — an unhandled exception in the background coroutine propagates to the event loop. Mitigated by a `try/except` wrapper that catches everything, logs, and marks the task FAILED in DB.
- Polling cancellation (DB flag) means up to one tool-round delay (~30s) between "stop" and actual stop.

### Option C: Subprocess

Start a separate Python process for each autonomous task. Full crash isolation. True parallelism on a multi-core VPS.

Tradeoffs:
- Cold start: ~2s per subprocess just for Python initialization, added to every task
- IPC complexity: serializing task state and results across processes (DB contention, RPC)
- Background process lifecycle: orphaned children, zombie reaping, process accounting

## Decision

Use `asyncio.create_task` to run autonomous tasks on the Telegram bot's event loop.

The blocking-httpx risk is acceptable because:
1. The sub-agent spends most of its time between LLM calls doing tool work (which yields to the event loop)
2. Cancellation via DB flag + polling covers the "user says stop" case with at most one tool round of lag
3. Thread safety with SQLite is already managed by the existing `timeout` + retry pattern in Crow's DB layer

A TaskRegistry dict `{task_id: asyncio.Task}` enables lifecycle management (cancel, list, status).

## Consequences

- Polling cancellation replaces instant cancellation. User sees "Task X cancelled" immediately but the sub-agent may complete one more tool round (~30s max).
- Sync httpx in the provider layer is NOT refactored to async. This decision can be revisited independently if blocking becomes a problem.
- The autonomous loop is opt-in via round-6 coaching trigger. Quick tasks stay inline, zero overhead.
