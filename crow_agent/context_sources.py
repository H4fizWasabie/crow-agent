"""Context sources — pluggable recall/context modules for the assembler.

Each source is registered with a name and tier. The assembler iterates
sources in tier order, calling each one with (user_input, db). Adding a
new context source means defining a function and registering it here —
the assembler interface never changes.

ponytail: narrow interface (2 params), deps captured via closure at registration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# Tier constants (match context budget system in CONTEXT.md)
TIER_REQUIRED = 1   # always included, fatal if exceeds budget
TIER_HIGH = 2       # included until 80% budget consumed
TIER_LOW = 3        # summarized to fit remaining headroom


@dataclass
class ContextSource:
    """A callable context source with metadata.

    name: human-readable label (e.g. "Skills", "Vault Pages")
    tier: one of TIER_REQUIRED, TIER_HIGH, TIER_LOW
    build: Callable[[str, Any], str | None] — takes (user_input, db), returns context text or None
    """
    name: str
    tier: int
    build: Callable[[str, Any], str | None]


def collect_context(
    sources: list[ContextSource],
    user_input: str,
    db: Any,  # CrowState
) -> tuple[list[str], list[str], list[str]]:
    """Run all sources and bucket results by tier.

    Returns (tier1_contexts, tier2_contexts, tier3_contexts).
    The assembler injects these into the prompt by tier priority.
    """
    tier1: list[str] = []
    tier2: list[str] = []
    tier3: list[str] = []

    for src in sources:
        try:
            result = src.build(user_input, db)
        except Exception:
            result = None  # ponytail: individual source failure is non-fatal
        if result:
            if src.tier == TIER_REQUIRED:
                tier1.append(result)
            elif src.tier == TIER_HIGH:
                tier2.append(result)
            else:
                tier3.append(result)

    return tier1, tier2, tier3

def register_default_sources(
    skills: Any,          # SkillsIndex
    memory_tracker: Any,  # MemoryTracker
    project_root: str,
) -> list[ContextSource]:
    """Build the default context source list.

    Sources are registered in the order they should be checked.
    Tier determines budget priority — see CONTEXT.md 'Context Budget System'.
    """
    from pathlib import Path
    import os, re

    _root = Path(project_root)
    sources: list[ContextSource] = []

    # ── Tier 1: Required ──

    # RULES.md
    def _build_rules(user_input: str, db: Any) -> str | None:
        rules_path = _root / "RULES.md"
        if rules_path.exists():
            return "\n\n## RULES\n" + rules_path.read_text().strip()
        return None
    sources.append(ContextSource("RULES.md", TIER_REQUIRED, _build_rules))

    # Matched skills (keyword + future semantic)
    def _build_skills(user_input: str, db: Any) -> str | None:
        matched = skills.match(user_input)
        if not matched:
            return None
        memory_tracker.note_matched_skills(
            [(s.name, s.source) for s in matched]
        )
        ctx = "\n\n## Matched Skills\n"
        for s in matched:
            ctx += f"### {s.name}\n"
            ctx += f"*[Required: start your response with `[Using: {s.name}]` when acting on this skill]*\n"
            ctx += "*Review the ## Usage Log below. If past outcomes show failures, adjust your approach.*\n"
            ctx += f"{s.body}\n"
        return ctx
    sources.append(ContextSource("Skills", TIER_REQUIRED, _build_skills))

    # ── Tier 2: High Priority ──

    # Recent learnings from memory tracker
    def _build_learnings(user_input: str, db: Any) -> str | None:
        return memory_tracker.inject_learnings()
    sources.append(ContextSource("Learnings", TIER_HIGH, _build_learnings))

    # Session resume context
    def _build_resume(user_input: str, db: Any) -> str | None:
        try:
            from .run_agent import _load_session_state
            return _load_session_state()
        except Exception:
            return None
    sources.append(ContextSource("Session Resume", TIER_HIGH, _build_resume))

    # Semantic search results
    def _build_semantic(user_input: str, db: Any) -> str | None:
        try:
            from .embeddings import semantic_search
            items: dict[str, str] = {}
            for s in skills.skills.values():
                items[f"skill:{s.name}"] = s.body[:500]
            _vault_root = _root / "memory vault"
            _idx_path = _vault_root / "index.md"
            if _idx_path.exists():
                for line in _idx_path.read_text().split("\n"):
                    if line.strip().startswith("- [") and "](" in line:
                        m = re.search(r'\(([^)]+\.md)\)', line)
                        if m:
                            _page = _vault_root / m.group(1)
                            if _page.exists():
                                items[f"vault:{m.group(1)}"] = _page.read_text()[:500]
            _sem_mtimes = skills.get_skill_mtimes()
            results = semantic_search(user_input, items, top_k=5, recheck_mtimes=_sem_mtimes)
            if results:
                ctx = "\n\n## Semantic Matches\n"
                for key, score in results:
                    _, item_id = key.split(":", 1)
                    ctx += f"- {item_id} (relevance: {score:.2f})\n"
                return ctx
        except Exception:
            pass
        return None
    sources.append(ContextSource("Semantic Search", TIER_HIGH, _build_semantic))

    # ── Tier 3: Low Priority ──

    # Vault index
    def _build_vault(user_input: str, db: Any) -> str | None:
        _vault_root = _root / "memory vault"
        _idx_path = _vault_root / "index.md"
        if not _idx_path.exists():
            return None
        idx_text = _idx_path.read_text(encoding="utf-8")
        return "\n\n📖 **Memory Vault Index** (wiki pages available):\n" + idx_text.strip()
    sources.append(ContextSource("Vault Index", TIER_LOW, _build_vault))

    # Tool budget notice
    def _build_budget_notice(user_input: str, db: Any) -> str | None:
        return (
            "\n\n📋 Tool Budget: max 20 tool calls, 12 rounds per turn. "
            "Batch independent calls in parallel. At round 4 you'll be offered "
            "spawn_agent delegation if the task needs deep work. "
            "See RULES.md Think in Code (#13-14) for tool hierarchy."
        )
    sources.append(ContextSource("Budget Notice", TIER_LOW, _build_budget_notice))

    return sources
