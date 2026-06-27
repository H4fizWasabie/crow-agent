---
name: improve-architecture
description: Find refactoring opportunities and improve codebase architecture
intent: development
triggers:
  - improve architecture
  - refactor
  - code smell
  - architectural improvement
  - reduce coupling
  - improve structure
  - refactoring opportunity
  - clean architecture
---

# Improve Codebase Architecture

Systematically find and fix architectural issues in the codebase.

## Scan Phase

1. **Identify coupling points**
   - Search for modules that import many others
   - Look for circular imports (check with `python -c "import sys; ..."`)
   - Flag god modules (single file >500 lines with mixed concerns)

2. **Check layering violations**
   - Do low-level modules import high-level modules?
   - Are there leaky abstractions (implementation details exposed in interfaces)?

3. **Naming and consistency**
   - Do function names match their actual behavior?
   - Are there multiple names for the same concept?

4. **Duplication**
   - Search for repeated patterns (copy-pasted code blocks)
   - Check for parallel hierarchies (similar class structures in different packages)

## Prioritize

For each finding, classify:
- **High**: Causes bugs or blocks features
- **Medium**: Increases cognitive load, slows development
- **Low**: Style/convention, may never matter

## Suggest Fixes

For each prioritized issue:
1. Describe the current problem with a specific example
2. Propose a concrete fix (rename, extract, split, move)
3. Estimate risk: safe (only test needs update), moderate (behavior preserved), risky (behavior changes)


## Usage Log
- [2026-06-21 04:55] outcome=not used
