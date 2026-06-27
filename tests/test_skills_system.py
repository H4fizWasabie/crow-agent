"""Tests for SkillsIndex: keyword matching, substring matching, edge cases."""

from __future__ import annotations

from crow_agent.skills_system import Skill, SkillsIndex


def _skill(name: str, triggers: list[str], desc: str = "") -> Skill:
    return Skill(
        name=name,
        description=desc,
        intent="",
        parameters={},
        triggers=triggers,
        body=f"# {name}\n{name} body",
        source=f"skills/{name}.md",
    )


def test_match_by_trigger_substring():
    """Skill matches when its trigger appears as substring."""
    idx = SkillsIndex(skills={"test": _skill("test", ["run tests"])})
    hits = idx.match("please run tests now")
    assert len(hits) == 1
    assert hits[0].name == "test"


def test_match_by_description():
    """Skill matches when description appears in input."""
    idx = SkillsIndex(skills={"lint": _skill("lint", ["check"], desc="lint code style")})
    hits = idx.match("can you lint code style please")
    assert len(hits) == 1
    assert hits[0].name == "lint"


def test_no_match():
    """No false positives for unrelated input."""
    idx = SkillsIndex(skills={"deploy": _skill("deploy", ["deploy"])})
    hits = idx.match("what's the weather")
    assert len(hits) == 0


def test_multiple_skills_match():
    """Multiple skills can match a single input."""
    idx = SkillsIndex(skills={
        "git": _skill("git", ["commit", "push"]),
        "deploy": _skill("deploy", ["deploy", "release"]),
    })
    hits = idx.match("commit and deploy")
    assert len(hits) == 2


def test_empty_triggers_no_match():
    """Skill with empty triggers never matches."""
    idx = SkillsIndex(skills={"empty": _skill("empty", [])})
    hits = idx.match("anything")
    assert len(hits) == 0
