"""Procedural skills system.

Skills are Markdown files with YAML front-matter. The agent scans skill directories
at startup, indexes front-matter (intent, params, triggers), and checks this index
before each turn — without loading full skill bodies into the prompt.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


from .paths import PROJECT_ROOT as _PROOT

DEFAULT_GLOBAL_SKILLS = Path.home() / ".crow_agent" / "skills"
DEFAULT_LOCAL_SKILLS = _PROOT / "skills"


@dataclass
class Skill:
    """A discovered skill with parsed metadata and full body."""
    name: str
    description: str
    intent: str
    parameters: dict[str, Any]
    triggers: list[str]
    body: str
    source: str  # file path


@dataclass
class SkillsIndex:
    """In-memory index of discovered skills. Local skills override global on name conflict."""
    skills: dict[str, Skill] = field(default_factory=dict)

    def match(self, user_input: str) -> list[Skill]:
        """Return skills whose description or triggers roughly match the user input.

        Uses substring + keyword overlap matching. Works well at ~20 skills.
        Past that ceiling, consider TF-IDF or semantic matching.
        """
        hits: list[Skill] = []
        lowered = user_input.lower()
        for skill in self.skills.values():
            if any(t.lower() in lowered for t in skill.triggers):
                hits.append(skill)
                continue
            if skill.description and skill.description.lower() in lowered:
                hits.append(skill)
                continue
            # Keyword overlap: check if any word from triggers appears
            trigger_words = set()
            for t in skill.triggers:
                trigger_words.update(t.lower().split())
            input_words = set(lowered.split())
            if len(trigger_words) >= 2 and len(trigger_words & input_words) >= 2:
                hits.append(skill)
            elif len(trigger_words) == 1 and trigger_words & input_words:
                hits.append(skill)
        return hits

    def get_skill_texts(self) -> dict[str, str]:
        """Return {name: body} for embedding precompute."""
        return {s.name: s.body for s in self.skills.values()}

    def get_skill_mtimes(self) -> dict[str, float]:
        """Return {filepath: mtime} for inline embedding recheck."""
        mtimes: dict[str, float] = {}
        for s in self.skills.values():
            try:
                mtimes[s.source] = Path(s.source).stat().st_mtime
            except OSError:
                pass
        return mtimes

    def summary(self) -> str:
        """Short summary of available skills for prompt injection (metadata only)."""
        lines = []
        for s in self.skills.values():
            lines.append(f"- **{s.name}**: {s.description} (triggers: {', '.join(s.triggers)})")
        return "\n".join(lines) if lines else "No skills loaded."


def parse_skill_file(path: str | Path) -> Skill | None:
    """Parse a skill markdown file with YAML front-matter.

    Expected format:
        ---
        name: my-skill
        description: What it does
        intent: the-goal
        parameters:
          foo: {type: string, required: true}
        triggers:
          - keyword1
          - phrase two
        ---
        # Skill Body
        Full instructions here...
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")

    # Extract YAML front-matter between --- delimiters
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
    if not m:
        return None

    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None

    if not meta or not isinstance(meta, dict):
        return None

    return Skill(
        name=meta.get("name", path.stem),
        description=meta.get("description", ""),
        intent=meta.get("intent", meta.get("name", path.stem)),
        parameters=meta.get("parameters", {}),
        triggers=meta.get("triggers", []),
        body=m.group(2).strip(),
        source=str(path),
    )


def scan_skills_dirs(
    global_dir: str | Path | None = None,
    local_dir: str | Path | None = None,
) -> SkillsIndex:
    """Scan skill directories and build the index. Local overrides global."""
    g_dir = Path(global_dir) if global_dir else DEFAULT_GLOBAL_SKILLS
    l_dir = Path(local_dir) if local_dir else os.environ.get(
        "CROW_AGENT_SKILLS_DIR", str(DEFAULT_LOCAL_SKILLS)
    )
    l_dir = Path(l_dir)

    index = SkillsIndex()

    # Global skills first
    if g_dir.exists():
        for md_file in sorted(g_dir.glob("*.md")):
            skill = parse_skill_file(md_file)
            if skill:
                index.skills[skill.name] = skill

    # Local skills override
    if l_dir.exists():
        for md_file in sorted(l_dir.glob("*.md")):
            skill = parse_skill_file(md_file)
            if skill:
                index.skills[skill.name] = skill

    return index


def register_tools(registry: Any) -> None:
    """Register skills-introspection tools."""

    @registry.register(description="List all available skills with descriptions and trigger keywords.")
    def list_skills() -> str:
        """Return a formatted list of all loaded skill names, descriptions, and trigger keywords."""
        idx = scan_skills_dirs()
        return idx.summary()

    @registry.register(description="Create a new reusable skill. Skills are step-by-step workflows that auto-trigger when the user says matching keywords. Saves to the skills folder and is available on the next conversation.")
    def create_skill(
        name: str,
        description: str,
        triggers: list[str],
        instructions: str,
    ) -> str:
        """Create a skill file at skills/{name}.md.

        Args:
            name: Short kebab-case name, e.g. 'weekly-report'
            description: One-line summary of what the skill does
            triggers: Keywords/phrases that should auto-trigger this skill
            instructions: Full markdown body — step-by-step instructions Crow should follow
        """
        if not re.match(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$', name):
            return f"Invalid name: '{name}'. Use kebab-case (letters, numbers, hyphens)."

        front = {
            "name": name,
            "description": description,
            "triggers": triggers,
        }
        # ponytail: no parameters/intent — YAGNI until a skill actually needs them
        body = f"---\n{yaml.dump(front, default_flow_style=False).strip()}\n---\n{instructions}\n"

        skills_dir = Path(os.environ.get("CROW_AGENT_SKILLS_DIR", str(DEFAULT_LOCAL_SKILLS)))
        skills_dir.mkdir(parents=True, exist_ok=True)
        filepath = skills_dir / f"{name}.md"
        filepath.write_text(body, encoding="utf-8")

        return f"Created skill '{name}' at {filepath}. Triggers: {triggers}. The skill will auto-load next session."

# ── Skill usage tracking (context management) ───────────────────────

_skill_usage: dict[str, int] = {}  # skill_name → last_referenced_turn


def _mark_skill_used(name: str, turn: int) -> None:
    """Record that a skill was referenced in this turn."""
    _skill_usage[name] = turn


def _get_skill_usage() -> dict[str, int]:
    """Return a copy of the skill usage map."""
    return dict(_skill_usage)


def _get_stale_skills(current_turn: int, max_idle: int = 3) -> list[str]:
    """Return skill names that haven't been referenced for max_idle turns."""
    stale = []
    for name, last_turn in _skill_usage.items():
        if current_turn - last_turn > max_idle:
            stale.append(name)
    return stale
