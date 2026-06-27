---
name: fulldev
description: Full development workflow — triage → design → implement → verify → commit → deploy → docs
intent: development
triggers:
  - fulldev
  - full dev
  - let's build
  - new feature
  - build workflow
  - development cycle
  - full pipeline
  - dev workflow
---

# FullDev Workflow

Standard development cycle. Follows phases sequentially. Skip phases based on triage.

---

## Phase 0: Triage

Determine scope before anything else.

Ask: _How big is this change?_

- **Trivial** — typo, single-line fix, obvious bug. → Skip to Phase 4 (Commit directly).
- **Small** — single concern, ≤3 files. → Implement → Verify → Commit.
- **Feature** — new capability, multi-file, architectural impact. → Full pipeline including Design + Docs.

---

## Phase 1: Design

Required for **Feature** tier. Optional for **Small** if design is unclear.

Interview the user until reaching shared understanding:
1. State what you think they want. Let them correct.
2. Surface trade-offs — don't hide confusion.
3. Propose approach. Get sign-off.
4. One question at a time — don't fire 5 questions at once.

Output: clear, agreed-upon approach with no ambiguity.

---

## Phase 2: Implement

Write code. Follow these rules:

1. **Surgical changes** — touch only what the request requires. Don't fix unrelated code.
2. **Simplicity first** — minimum code that solves the problem. No speculative abstractions.
3. **Goal-driven** — define success before writing. For bugs: write repro test first.
4. **Read before edit** — understand existing code before touching it.

After implementation:
- Remove imports/variables made unused by YOUR changes.
- Do NOT remove pre-existing dead code.

---

## Phase 3: Verify

| Tier | Verification |
|------|-------------|
| Trivial | No verification needed |
| Small | Run related tests: `python -m pytest tests/ -x -q` (adjust command to your project) |
| Feature | Spawn verification agent with: original task, files changed, approach. Fix until PASS. Then spot-check 2-3 outputs from verify report. |

After code changes, update any project index or knowledge graph if your project uses one.

---

## Phase 4: Commit

Standard git commit flow:
1. `git status` + `git diff` — check what changed
2. Review recent commits for message style
3. Stage specific files — NOT `git add -A` (avoid staging secrets or unrelated changes)
4. Commit with descriptive message
5. `git status` to verify

Do NOT commit: `.env`, `credentials.json`, `*.key`, or any file containing secrets.

---

## Phase 5: Deploy

**Optional.** Skip if your project doesn't need deployment (library, docs-only, local tool).

If project has a deploy pipeline:
1. Run the project's deploy command
2. Verify it's running (health check, smoke test)
3. If deploy fails, report the error — don't attempt auto-fix

---

## Phase 6: Docs

Update documentation for architectural changes:

1. If design or behavior changed — update relevant docs
2. If new technical debt was introduced — note it somewhere visible
3. If a significant decision was made — record the trade-off and rationale
4. Update changelog if the project uses one

Skip if change has no architectural impact (typo, test-only, cosmetic).
