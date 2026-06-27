---
description: "Scan the memory vault for health issues — index mismatches, missing frontmatter, broken cross-refs, missing raw sources, orphan pages. Optional fix mode."
name: vault-lint
triggers:
- lint vault
- vault health
- check vault
- scan vault
- vault lint
---
# Vault Lint

Check the memory vault for 5 categories:

1. **Index mismatches** — pages in `wiki/pages/` not in `index.md`, or in index but missing from disk
2. **Missing frontmatter** — wiki pages without valid YAML frontmatter (type, title, created, tags, sources)
3. **Broken cross-refs** — [[wikilinks]] that don't resolve to existing pages
4. **Missing raw sources** — wiki pages with `sources:` frontmatter pointing to files not in `raw/sources/`
5. **Orphan pages** — pages with no incoming links from other pages or index.md

## Process

1. Read `index.md` — extract listed pages
2. List files in `wiki/pages/` and `raw/sources/`
3. For each wiki page, read frontmatter (first 20 lines)
4. Check each category above
5. Report results in a clean table

## Report Format

```
## Vault Lint Report
- Status: ✅ CLEAN / ⚠️ ISSUES FOUND
- Pages checked: N
- Issues: N
```

If user says "fix", attempt automatic fixes:
- Add missing `created` date to frontmatter
- Add missing `type` field (default: "entity")
- Update `index.md` to include missing pages
- Report what was fixed


## Usage Log
- [2026-06-20 06:23] outcome=not used
- [2026-06-21 04:30] outcome=not used
