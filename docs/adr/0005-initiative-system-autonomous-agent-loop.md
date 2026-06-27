# ADR 0005 — Initiative System: Autonomous Agent Loop

## Context

Crow has two execution paths that never connect:
- Agent loop (run_agent.py): user-triggered. RECALL to ASSEMBLE to CALL to TOOL_LOOP to RESPOND.
- Heartbeat (heartbeat_engine.py): idle observer. INFORM or INVESTIGATE or PROCESS or REFLECT or NOTHING.

Heartbeat slices 4-7 attempt autonomous action (drain delegates, self-code fixes, TODO scanning) but hit a wall: they cannot invoke the agent loop. Heartbeat writes notes to self in __autonomous__ session; Crow never acts on them without user input.

User wants Crow to run itself and report to me while retaining full control via chat.

## Decision

Introduce Initiative — a mechanism where Heartbeat detects problems and triggers full agent turns without user input.

### Core concepts

**Initiative:** The mechanism that takes Heartbeat findings and triggers a full agent turn without user input.

**Crow Log:** Dedicated Telegram channel for autonomous turn output. User chat stays clean.

**Initiative ID:** UUID assigned per Initiative turn. Format: [#abc123]. Links across Heartbeat to Initiative to Crow Log to user follow-up.

### Architecture

Heartbeat (observer, ticks every 3-6 min) detects events. On ACT, spawns Initiative.

Initiative (actor) creates new AIAgent with session_id as initiative_UUID. Reads last 3 Heartbeat ticks from autonomous session for context. Runs full agent loop. Output goes to Crow Log channel.

### Trigger classification

- Test failure: ACT. Full agent turn with context chain.
- Cron failure: ACT. Investigate and fix.
- Pending delegate task: ACT. Drain queue without waiting.
- Git changes: INFORM only.
- New reports: INFORM only.
- Overdue reminders: INFORM only (handled by ReminderEngine).

## Key design rules

### 1. Separate sessions, concurrent execution
Heartbeat skip guard REMOVED. Initiative spawns new AIAgent with separate session_id. No lock collision (asyncio.Lock per chat_id). SQLite WAL mode supports concurrent access. 9 provider keys prevent contention.

### 2. Heartbeat writes goal, Initiative executes
Heartbeat cheap LLM (opencode-zen, deepseek-v4-flash) writes one-line goal. Initiative feeds it directly as trigger.prompt. No duplicated RECALL or context assembly.

### 3. Rate limiting
- max_initiative_turns_per_hour = 2 (global cap).
- After 3 consecutive Initiative turns without user interaction: pause, send prompt to Crow Log.
- Initiative pauses crew delegation while user is chatting.

### 4. All tools allowed, no restrictions
No tool whitelist. Can edit source code, commit, push, send email, contact suppliers, spawn workers. Guarded by rate limits, not tool blocks.

### 5. Initiative survives stop
User stop kills user agent turn only. Initiative continues independently. Separate sessions, separate lifecycle.

### 6. Output channel
Autonomous turn results go to Crow Log (dedicated Telegram channel). Each message tagged with initiative ID. User can query what happened with a specific ID.

## Benefits

- Unified loop: Heartbeat triggers, Initiative acts. Same state machine, two entry points.
- No duplication: Heartbeat detects (cheap model), Initiative solves (main model).
- User in control: Chat overrides nothing. Initiative pauses delegation when user active.
- Self-healing: Test failures and cron issues fixed without user noticing.

## Consequences

### Code changes
- heartbeat_engine.py: add ACT decision verb, remove skip guard, route slice 6 through Initiative
- run_agent.py: accept trigger_source parameter
- task_registry.py: scope drain_and_execute by chat_id
- telegram_bot.py: add Crow Log channel handler
- context_assembler.py: Initiative reads heartbeat autonomous session for RECALL
- memory_tracker.py: remove skip for autonomous turns

### Risk
- Concurrent git commit from two agents possible (low probability). Mitigate with file lock if needed.
- Provider key exhaustion if rate limit fails (9 keys, 2 turns/hour: ample headroom).

## Rejected options

- Heartbeat grows slice 8: conflates observer and actor. Separate concepts clearer.
- Tool whitelist for Initiative: user rejected. All tools allowed.
- Initiative generates own goal: duplicates heartbeat work. More tokens, same outcome.
- OpenRouter for heartbeat decisions: user rejected. Use opencode-zen with existing keys.
