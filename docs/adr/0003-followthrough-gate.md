# FollowThrough Gate — Post-Tool-Loop Delivery Verification

**Status: SUPERSEDED** (2026-06-17, commit db29691)

Replaced by internal monologue design (c83a302): LLM text without tools is
preserved as context rather than discarded. Gate-based delivery verification
was removed in favor of LLM-owned quality control. The hybrid code+LLM check
(Option D) was never reliable — LLM rationalized its own failures. Internal
monologue with honest correction messages works better.

See `crow_agent/run_agent.py` RESPOND phase: "no gates — LLM owns quality".

---

## Original ADR (historical)

Crow exhibited two failure patterns:

1. **Narrated intent**: LLM says "let me fetch X" as text without calling any tool. The loop sees no `tool_calls` and exits — no action taken.
2. **Done without delivery**: Tool called (e.g., web_fetch), data retrieved, but LLM responds with "Done" or "Saved" instead of presenting results to user.

Both patterns violate user trust: Crow sounds proactive and capable but doesn't follow through. The existing VERIFY phase only checks code correctness — it has no delivery-awareness.

## Options Considered

### Option A: Prompt Rules Only

Add system prompt instructions: "Never narrate intent — call tools directly. Always present results in your response."

Pros: Zero lines of code. Cheap (+50 tokens/turn).

Cons: LLM compliance unreliable. Same LLM that made the mistake would enforce the rule. No enforcement.

### Option B: Pure LLM Self-Judge

After tool loop, send the full turn context (user input, tool calls + outputs, draft response) to the LLM with a rubric: "Did you deliver what was asked?"

Pros: Flexible, context-aware. Catches semantic violations.

Cons: +1 extra LLM call per turn — 2× cost. LLM can rationalize away its own failures.

### Option C: Pure Code Heuristic

Parse tool names, count output bytes, check response length and keywords.

Pros: Zero LLM cost. Deterministic.

Cons: No context understanding. Flags "don't bother showing" as violation. Misses implied multi-turn promises.

### Option D: Hybrid — Code Filter + Conditional LLM (chosen)

Fast structural check (tool classification + response heuristic) catches obvious violations at zero cost. If code can't decide, flag for LLM judgment.

Pros: Zero cost on the common case (clean deliveries). Catches the exact failure patterns reported. LLM only invoked on flagged turns (~10% estimate). No re-engineering of provider layer (no response_format dependency).

Cons: More code than pure prompt rules (~80 lines). Tool role list needs maintenance as tools change.

## Decision

Implement Option D: hybrid FollowThrough gate in the RESPOND phase.

### Key Design Points

- **Location**: Inside RESPOND, after tool loop exits, before saving to DB. Same pattern as VERIFY but for delivery.
- **Tool classification**: Consumer-facing (must present) vs Action-only (execution is delivery) vs Dual.
- **Response substance check**: ≥80 chars of non-boilerplate OR contains URLs/code blocks/data. Catches "done/saved/okay" responses.
- **Re-entry ceiling**: Delivery violations = 1 retry. No-tool violations = 2 retries.
- **Intent inferred from tool calls**: No pre-classification of user message. If consumer-facing tools ran, the check runs. Simple, no extra calls.
- **Pattern 1 mitigation**: Prompt rule (narrated intent → tool not called) combined with no-tool violation gate as backup.

### Non-Design

- No `response_format` / structured output — provider layer uses custom httpx calls without structured output support. Adding it would require per-provider implementation with no clear benefit over the hybrid approach.
- No pre-turn intent classification — adds complexity for marginal gain. Post-hoc inference covers the real-world cases.

## Consequences

1. **+1 extra LLM call on flagged turns (est. 10% of turns)**. Acceptable — deepseek-v4-flash is cheap ($0.15/M tokens).
2. **Response delay on flagged turns**. +1-2s for the re-entry round. Rare enough not to matter.
3. **False positives**: user asks "fetch and save" → tools run → response "saved to vault" flagged as vague. LLM would re-present and annoy user. Mitigated by action-only tool classification (remember/learn = action, skip check).
4. **Prompt rule still needed for Pattern 1**. The gate catches "no tools called" after the fact, but a good prompt rule prevents the LLM from narrating in the first place.
5. **Tool role list is a maintenance burden**. New tools must be classified. Mitigated by defaulting unknown tools to "consumer-facing" (safe side — extra check is better than missed detection).

## Later Revisions

### 2026-06-14 — FT Propels: Tool Injection Instead of Re-Try

**Problem:** The original FT retry (re-call LLM with system message) failed. The LLM kept narrating intent even after FT's system message. FT was punishing the LLM for a capability gap instead of solving it.

**Fix:** Added `_inject_tool_call()` — when CASE 2 triggers (no tools called, user asked for action), FT maps user keywords to a tool call and injects it directly. The while-loop re-enters the tool loop with the synthetic call. Tool executes, result comes back, LLM gets it in context and delivers.

Also added:
- **Cross-turn resume** (`_ft_pending_resume` flag) — when FT exhausts retries, next turn gets a system message telling Crow to resume the original task. Prevents "On it." dead end.
- **Intent narration detection** (`_is_intent_narration`) — CASE 1 now catches "let me/I'll do X" even when text is long, by checking for intent phrases without data indicators.
- **Stronger prompt rules** — "never narrate intent" raised to CRITICAL tier with bad/good examples.

**Result:** FT is no longer a wall that blocks — it's a funnel that propels Crow back into action. Three layers: prompt (prevent) → injection (propel) → resume (recovery).

### 2026-06-14 — "On it." Removed, Dead End Eliminated

**Problem:** "On it." exhaustion fallback was worse than the violation. User: *"i thought FT is supposed to encourage crow to be proactive not saying on it anymore?"*

**Fix:** Removed all "On it." overrides. FT exhaustion now passes original response through — even vague/intent-narration is better than silence. Cross-turn resume (`_ft_pending_resume`) still set so next turn gets context to complete.

**Working state confirmed** in real conversation — Crow delivered full explanations and database audit via injection + resume chain. No dead ends at any FT stage.
