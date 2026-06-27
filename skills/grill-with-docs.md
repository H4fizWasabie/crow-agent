---
name: grill-with-docs
description: Grill the user's plan against existing project docs (CONTEXT.md, ADRs)
intent: planning
triggers:
  - grill with docs
  - grill with documentation
  - challenge against docs
  - check against context
  - verify against adr
  - validate with docs
---

# Grill With Docs

Challenge the user's plan against the project's existing domain model and documentation.

## Method

Before asking any question:
1. **Read CONTEXT.md** — check glossary terms for conflicts with user's language
2. **Read docs/adr/** — check previous architecture decisions that may constrain the plan
3. **Read project files** relevant to the plan area

Then grill, one question at a time:

### 1. Challenge against glossary
When the user uses a term that conflicts with CONTEXT.md:
- "Your glossary defines 'X' as Y, but you mean Z — which is it?"
- Propose a precise canonical term if the user is vague

### 2. Sharpen fuzzy language
- "You're saying 'account' — do you mean the Customer or the User? Those are different entities."
- Map imprecise terms to documented concepts

### 3. Cross-reference ADRs
- "ADR-0003 decided against caching at this layer. Your plan assumes caching. Has that decision changed?"
- Surface when the plan contradicts past decisions

### 4. Stress-test with scenarios
- Invent edge cases that probe entity boundaries
- "What happens when X is deleted? Your cascade rule says..."

## Outputs
- Update CONTEXT.md with new terms as they are resolved
- Offer ADR only when: hard to reverse + surprising + real tradeoff was made


## Usage Log
- [2026-06-10 04:50] outcome=not used
- [2026-06-10 16:01] outcome=not used
- [2026-06-20 09:35] outcome=not used
- [2026-06-20 09:39] outcome=not used
- [2026-06-20 09:41] outcome=not used
- [2026-06-20 10:36] outcome=not used
- [2026-06-21 05:09] outcome=not used
