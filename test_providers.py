"""End-to-end test: both providers, multi-turn, tool calls."""

from __future__ import annotations
import os, json, sys
from crow_agent.providers import resolve_provider, ChatMessage, ChatResponse

passed = 0
failed = 0

def check(label: str, cond: bool, detail: str = ""):
    global passed, failed
    status = "✓" if cond else "✗"
    print(f"  {status} {label}" + (f" — {detail}" if detail else ""))
    if cond:
        passed += 1
    else:
        failed += 1


# ── OpenCode Go ──────────────────────────────────────────────

print("\n═══ OpenCode Go (deepseek-v4-flash) ═══\n")

os.environ["OPENCODE_GO_API_KEY"] = os.environ.get("OPENCODE_GO_API_KEY", "sk-test-placeholder")
os.environ["OPENCODE_GO_BASE_URL"] = "https://opencode.ai/zen/go/v1"
os.environ["OPENCODE_GO_MODEL"] = "deepseek-v4-flash"

try:
    p1 = resolve_provider("opencode_go")
    print(f"  Model: {p1.config.model}")

    # Turn 1: basic
    r1 = p1.chat([ChatMessage(role="user", content="Reply with exactly: OPENCODE_OK")], max_tokens=20)
    check("Basic response", "OPENCODE_OK" in r1.content, r1.content[:80])

    # Turn 2: multi-turn
    msgs = [
        ChatMessage(role="user", content="My name is Alice. What's my name?"),
    ]
    r2 = p1.chat(msgs, max_tokens=30)
    check("Multi-turn memory", "Alice" in r2.content, r2.content[:80])

    # Turn 3: tool call
    tools = [{
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get current time",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    r3 = p1.chat(
        [ChatMessage(role="user", content="What time is it? Use the tool.")],
        tools=tools,
        max_tokens=100,
    )
    has_tool_calls = len(r3.tool_calls) > 0
    check("Tool call", has_tool_calls, str(r3.tool_calls)[:100] if has_tool_calls else r3.content[:80])

    # Usage tracking
    check("Usage reported", r1.usage.get("total_tokens", 0) > 0, str(r1.usage))

except Exception as e:
    print(f"  ✗ Exception: {e}")
    failed += 4


# ── Command Code ─────────────────────────────────────────────

print("\n═══ Command Code (xiaomi/mimo-v2.5-pro) ═══\n")

os.environ["COMMANDCODE_API_KEY"] = os.environ.get("COMMANDCODE_API_KEY", "sk-test-placeholder")
os.environ["COMMANDCODE_BASE_URL"] = "https://api.commandcode.ai/provider/v1"
os.environ["COMMANDCODE_MODEL"] = "xiaomi/mimo-v2.5-pro"

try:
    p2 = resolve_provider("commandcode")
    print(f"  Model: {p2.config.model}")

    # Turn 1: basic
    r1 = p2.chat([ChatMessage(role="user", content="Reply with exactly: COMMANDCODE_OK")], max_tokens=20)
    check("Basic response", "COMMANDCODE_OK" in r1.content, r1.content[:80])

    # Turn 2: multi-turn
    r2 = p2.chat([ChatMessage(role="user", content="Count from 1 to 5")], max_tokens=50)
    check("Counting", all(str(i) in r2.content for i in [1, 2, 3]), r2.content[:80])

    # Turn 3: tool call
    r3 = p2.chat(
        [ChatMessage(role="user", content="Read the file SOUL.md using the read tool.")],
        tools=tools,
        max_tokens=200,
    )
    has_tool_calls = len(r3.tool_calls) > 0
    check("Tool call", has_tool_calls, str(r3.tool_calls)[:100] if has_tool_calls else r3.content[:80])

    # Usage tracking
    check("Usage reported", r1.usage.get("total_tokens", 0) > 0, str(r1.usage))

except Exception as e:
    print(f"  ✗ Exception: {e}")
    failed += 4


# ── Summary ──────────────────────────────────────────────────

print(f"\n═══ Results: {passed} passed, {failed} failed ═══\n")
sys.exit(1 if failed else 0)
