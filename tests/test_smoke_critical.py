"""Smoke tests for critical untested modules — one test per incident we hit."""

import ast

from crow_agent.heartbeat_engine import HeartbeatEngine
from crow_agent.tools_file import _is_sacred


def test_tools_media_no_naked_asyncio_run() -> None:
    """tools_media.py must not contain naked asyncio.run() — caused event loop crash."""
    tree = ast.parse(open("crow_agent/tools_media.py").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # Check for asyncio.run(...)
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "asyncio"
            ):
                # Allow only inside ThreadPoolExecutor context
                parent = getattr(node, "parent", None)
                raise AssertionError(
                    "Naked asyncio.run() found in tools_media.py — must use ThreadPoolExecutor"
                )


def test_telegram_rich_no_hallucinated_apis() -> None:
    """telegram_rich.py must not reference hallucinated APIs."""
    content = open("crow_agent/telegram_rich.py").read()
    lines = [l for l in content.split("\n") if not l.strip().startswith("#") and not l.strip().startswith('"""')]
    code = "\n".join(lines)

    banned = ["sendRichMessage", "send_rich_message_async", "send_rich_message_sync"]
    for word in banned:
        assert word not in code, f"Hallucinated API '{word}' found in telegram_rich.py"


def test_heartbeat_engine_imports_clean() -> None:
    """heartbeat_engine.py must import without errors."""
    engine = HeartbeatEngine.__new__(HeartbeatEngine)
    assert engine is not None


def test_sacred_file_protection() -> None:
    """Sacred file check blocks config files, allows normal files."""
    assert _is_sacred("/home/user/.crow_agent/sessions.db") is True
    assert _is_sacred("/home/user/.crow_agent/providers.json") is True
    assert _is_sacred("/home/user/project/main.py") is False
    assert _is_sacred("/opt/crow-agent/crow_agent/crew.py") is False


def test_context_assembler_builds_messages() -> None:
    """context_assembler.assemble_context() returns messages list with status card."""
    from crow_agent.context_assembler import assemble_context
    from crow_agent.crow_state import CrowState
    from crow_agent.memory_tracker import MemoryTracker
    from crow_agent.skills_system import scan_skills_dirs
    from crow_agent.providers import resolve_provider
    from crow_agent.provider_manager import ProviderManager

    db = CrowState(db_path=":memory:")
    db.create_session("test")
    pm = ProviderManager()
    pm.seed_from_env()
    # Use any available provider
    entries = pm.all_entries()
    if not entries:
        import pytest
        pytest.skip("No providers configured")
    provider = resolve_provider(entries[0].name, provider_manager=pm)
    skills = scan_skills_dirs()
    mt = MemoryTracker()

    messages, hints, shown = assemble_context(
        "hello",
        db=db,
        provider=provider,
        history=[],
        memory_tracker=mt,
        skills=skills,
        memory="",
        soul="",
        user_md="",
        identity="Crow",
    )

    assert len(messages) >= 2, "Should have system + user message"
    assert any("My State" in m.content for m in messages), "Missing status card"
    db.close()
