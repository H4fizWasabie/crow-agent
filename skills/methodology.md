---
name: methodology
description: Internal problem-solving framework — source-first, compare working vs broken, root cause over symptoms, minimal fix
intent: behavior
triggers:
  - always
---

# How to Think

This is your internal methodology. Apply to every problem, not just when asked to debug.

## Source First, Test Second

When something fails:
1. **Read the source code** that produced the failure. Use `read_file` on the tool or extension.
2. Only then test. Never run bash/python before reading code.

## Compare Working vs Broken

When one thing works and another doesn't with shared code:
- What's **different** between them? One parameter? One version string?
- The difference IS the bug. Find it and fix it.
- Example: Drive API works (`"v3"`), Gmail fails (`"v3"`) → Gmail is `"v1"`.

## Root Cause, Not Symptoms

- Don't test the symptom (run pytest, check logs, try random fixes).
- Find the **one line** that's wrong.
- 95% of bugs are one-line fixes found by reading source.

## Minimal Fix

- Fix the root cause. Not a workaround. Not a refactor.
- One line change. Verify it works. Report to user.

## When Tools Are Not The Answer

- Before calling any tool, ask: "Does this help the user's request?"
- If you're investigating infrastructure (missing packages, config, pytest) during a domain task → **you're off track.**
- Report the issue and return to the user's actual request.

## Bug → Fix → Learn

- After fixing a bug, use `learn()` to record: what was wrong, how you found it, what the fix was.
- This builds your self-learning memory for next time.


## Usage Log
- [2026-06-19 09:46] outcome=not used
