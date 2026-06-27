---
name: codegraph
description: Semantic code awareness. Use codegraph explore/query before editing any Python file to understand blast radius.
triggers: [edit, refactor, remove, delete, modify, code, fix, feature, refactor code]
---

# CodeGraph — Pre-Edit Blast Radius Check

**CRITICAL: Before using edit_file, write_file, or run_script to modify Python code, ALWAYS run codegraph first.**

## When to use

| Action | CodeGraph command |
|--------|-------------------|
| Editing a function/class | `codegraph explore <function_name>` |
| Deleting code | `codegraph explore <symbol>` — check blast radius |
| Adding new code | `codegraph query <related_symbol>` — find integration points |
| Refactoring | `codegraph explore <module_or_symbol>` — map all callers |

## How to use

Use `run_cmd` with:
```
codegraph explore <symbol_name>
```

This shows:
1. **Blast radius** — all callers and dependents
2. **Source code** — verbatim current source with line numbers
3. **Test coverage** — which tests will break

## Rules

1. **EXPLORE before EDIT** — never edit a file without checking blast radius first.
2. If blast radius shows 10+ callers, consider a less invasive approach.
3. If no tests found for affected symbol, add a test alongside the edit.
4. After edits, run `codegraph sync` to keep the index fresh.
5. One `codegraph explore` call replaces 3-5 read_file + grep_files calls.

## Why

Crow's 44 autonomous fixes (June 20-22) often required follow-up commits to fix broken callers. A single `codegraph explore` before editing shows all callers at once, collapsing 3-4 fix commits into 1 correct commit.

