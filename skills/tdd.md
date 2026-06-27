---
name: tdd
description: Test-driven development with red-green-refactor loop
intent: development
triggers:
  - tdd
  - red green
  - test first
  - test-driven
  - write tests
  - make it pass
  - refactor
  - test driven development
---

# TDD Workflow

Follow the red-green-refactor cycle strictly. Do not combine phases.

## Red Phase
1. Write a failing test that captures the expected behavior
2. Run the test — confirm it fails (red)
3. Report what the test expects

## Green Phase
1. Write the minimum code to make the test pass
2. Do not add extra functionality beyond what the test requires
3. Run the test — confirm it passes (green)

## Refactor Phase
1. Clean up both test and implementation
2. Remove duplication, improve naming, simplify
3. Run the test again — confirm it still passes
4. Report what was refactored

## Rules
- Never write production code without a failing test first
- Never skip the refactor phase
- Keep tests isolated — no shared state between tests
- One assertion pattern per test when possible


## Usage Log
- [2026-06-10 04:54] outcome=not used
- [2026-06-10 07:54] outcome=not used
- [2026-06-20 10:47] outcome=not used
- [2026-06-20 11:02] outcome=not used
- [2026-06-21 04:55] outcome=not used
- [2026-06-21 05:09] outcome=not used
