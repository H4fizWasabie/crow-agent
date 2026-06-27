"""Full agent e2e: OpenCode Go + tools + multi-turn + FTS recall."""

from __future__ import annotations
import os, tempfile, sys

os.environ["COMMANDCODE_API_KEY"] = os.environ.get("COMMANDCODE_API_KEY", "sk-test-placeholder")
os.environ["COMMANDCODE_BASE_URL"] = "https://api.commandcode.ai/provider/v1"
os.environ["COMMANDCODE_MODEL"] = "xiaomi/mimo-v2.5-pro"

from crow_agent.run_agent import AIAgent, State, Trigger, TriggerSource
from crow_agent.toolsets import ToolRegistry
from crow_agent.model_tools import register_builtins
from crow_agent.crow_state import CrowState

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

print("\n═══ Agent E2E: OpenCode Go + Tools + Multi-turn + FTS ═══\n")

with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
    db_path = f.name

try:
    tools = ToolRegistry()
    register_builtins(tools)

    agent = AIAgent(
        session_id="e2e-test",
        provider_name="commandcode",
        db_path=db_path,
        tool_registry=tools,
        soul_path="SOUL.md",
        user_path="USER.md",
        memory_path="MEMORY.md",
    )

    check("Agent created", agent.state == State.IDLE)
    check("Tools loaded", len(tools._tools) == 7, f"{len(tools._tools)} tools")

    # Turn 1: simple
    print("\n  → Turn 1: Simple greeting")
    r1 = agent.run(Trigger(source=TriggerSource.USER, prompt="Say hello in 5 words or less."))
    check("Turn 1 response", len(r1) > 0, r1[:80])
    check("State back to IDLE", agent.state == State.IDLE)

    # Turn 2: use a tool (read file)
    print("\n  → Turn 2: Read SOUL.md")
    r2 = agent.run(Trigger(source=TriggerSource.USER, prompt="Read the SOUL.md file and tell me the first guideline."))
    check("Turn 2 used tool", "terse" in r2.lower() or "concise" in r2.lower() or "brief" in r2.lower(), r2[:120])

    # Turn 3: FTS recall — reference something from turn 1
    print("\n  → Turn 3: FTS recall")
    r3 = agent.run(Trigger(source=TriggerSource.USER, prompt="What did I ask you in my very first message to you?"))
    check("FTS recall", "hello" in r3.lower() or "5 words" in r3.lower(), r3[:120])

    # Turn 4: another tool (run command)
    print("\n  → Turn 4: Run shell command")
    r4 = agent.run(Trigger(source=TriggerSource.USER, prompt="Run the command: echo AGENT_WORKS"))
    check("Shell tool", "AGENT_WORKS" in r4, r4[:120])

    # Verify DB persistence
    db = CrowState(db_path)
    hist = db.history("e2e-test")
    db.close()
    check("DB has 8+ turns", len(hist) >= 8, f"{len(hist)} turns")  # 4 user + 4 assistant

    # Skills loaded
    check("Skills available", len(agent.skills.skills) >= 1)

    agent.close()
    check("Agent closed cleanly", True)

except Exception as e:
    import traceback
    traceback.print_exc()
    failed += 1

print(f"\n═══ Results: {passed} passed, {failed} failed ═══\n")
sys.exit(1 if failed else 0)
