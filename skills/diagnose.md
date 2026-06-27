---
name: diagnose
description: Disciplined debugging loop. Stop retrying — read source, compare working vs broken, fix root cause.
intent: behavior
triggers:
  - debug
  - not working
  - error
  - why isn't
  - stuck
  - loop
  - self-repair
  - fix extension
---

# Diagnose — Stop Retrying, Read Source

When something fails, follow this loop. Never retry the same failing approach more than twice.

## The Loop

1. **Reproduce** — What exactly fails? Get the exact error.
2. **Minimise** — What differs between working and broken? If similar code works (Drive API) but this doesn't (Gmail API), the difference IS the bug.
3. **Read source first** — Before testing externally, read the code that constructs the failing component. Use `read_file` on the extension or tool code.
4. **Compare** — Working component vs broken component. What's different? One line? One parameter?
5. **Fix** — Change the root cause, not a workaround.
6. **Test** — Verify the fix works before reporting to user.

## Loop Detection

- If 3+ consecutive tool calls fail with similar errors → YOU ARE IN A LOOP.
- Stop, re-read the source, compare working vs broken.
- Never run more bash tests without first reading the code.

## Working vs Broken

When one API/service works and another doesn't:
```
Working: Drive API → build("drive", "v3", ...)
Broken:  Gmail API → build("gmail", "v3", ...)
                            ↑ same version string
```
Gmail API is v1, not v3. One-line fix.

This pattern repeats everywhere — the difference between working and broken is a single line or parameter.


## Usage Log
- [2026-06-19 05:32] outcome=not used
- [2026-06-20 09:39] outcome=not used
- [2026-06-20 09:48] outcome=not used
- [2026-06-21 03:28] outcome=not used
- [2026-06-21 04:55] outcome=not used
- [2026-06-21 04:55] outcome=not used
- [2026-06-21 05:09] outcome=not used
- [2026-06-21 05:09] outcome=not used
