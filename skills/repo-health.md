---
name: repo-health
description: Run repository health checks — lint, test, and git status
intent: health-check
triggers:
  - health check
  - repo health
  - check repo
  - project status
parameters:
  path:
    type: string
    required: false
    default: "."
---
# Repo Health Check

1. Run `git status` to check for uncommitted changes
2. Run `git log --oneline -5` for recent commits
3. Run `python -m pytest --co -q 2>/dev/null || echo "No pytest tests found"`
4. Check disk usage: `du -sh .`
5. Report findings concisely


## Usage Log
- [2026-06-21 05:09] outcome=not used
