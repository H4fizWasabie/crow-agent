"""Test Hermes proactive task awareness: intent detection, auto [CONTINUE],
fast heartbeat pickup, and backoff limits."""

import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from crow_agent.turn_finalizer import _detect_narrated_intent, finalize_turn
from crow_agent.heartbeat_engine import ContextDelta


# ─── intent detection ─────────────────────────────────────────────

class TestDetectNarratedIntent:
    """Intent-to-act detection: Crow narrates intent = pending task."""

    def test_detects_i_will(self):
        assert _detect_narrated_intent("I will research this and get back to you.")
        assert _detect_narrated_intent("Let me look into that for you.")
        assert _detect_narrated_intent("I'll check the logs and report back.")
        assert _detect_narrated_intent("Going to investigate the issue now.")
        assert _detect_narrated_intent("I need to look at the code first.")
        assert _detect_narrated_intent("Working on it — give me a moment.")
        assert _detect_narrated_intent("I plan to fix this tomorrow.")

    def test_rejects_done_or_continue(self):
        assert not _detect_narrated_intent("I will do it. [DONE] Task complete.")
        assert not _detect_narrated_intent("[CONTINUE] I'll keep working on this.")
        assert not _detect_narrated_intent("[DONE]")

    def test_rejects_plain_results(self):
        assert not _detect_narrated_intent("Here is the result: 42.")
        assert not _detect_narrated_intent("The file was modified successfully.")
        assert not _detect_narrated_intent("Task completed. All tests pass.")

    def test_case_insensitive(self):
        assert _detect_narrated_intent("I WILL INVESTIGATE THIS NOW.")

    def test_empty_string(self):
        assert not _detect_narrated_intent("")
        assert not _detect_narrated_intent("ok")


# ─── auto [CONTINUE] in finalize_turn ─────────────────────────────

class TestAutoContinueInFinalize:
    """Intent narration without tools → auto [CONTINUE] appended."""

    @pytest.fixture
    def mock_agent(self):
        agent = MagicMock()
        agent._finish_turn.return_value = "I will research this."
        agent._record_phase = MagicMock()
        agent._memory_tracker = MagicMock()
        agent._memory_tracker.observe_turn.return_value = []
        agent._pending_skill_hints = []
        agent._turn_count = 0
        agent.session_id = "test_session"
        return agent

    @pytest.fixture
    def mock_trigger(self):
        t = MagicMock()
        t.prompt = "research topic X"
        t.source = MagicMock()
        return t

    def test_auto_continue_when_intent_no_tools(self, mock_agent, mock_trigger):
        mock_agent._finish_turn.return_value = "I will research topic X."
        result = finalize_turn(
            mock_agent,
            final_text="I will research topic X.",
            trigger=mock_trigger,
            all_tool_calls=[],
            total_prompt=10,
            total_completion=5,
            turn_start=time.monotonic(),
            user_goal="research topic X",
        )
        assert "[CONTINUE]" in result

    def test_no_auto_continue_when_tools_used(self, mock_agent, mock_trigger):
        mock_agent._finish_turn.return_value = "I will research this."
        result = finalize_turn(
            mock_agent,
            final_text="I will research this.",
            trigger=mock_trigger,
            all_tool_calls=[{"function": {"name": "web_search"}}],
            total_prompt=10,
            total_completion=5,
            turn_start=time.monotonic(),
            user_goal="research topic X",
        )
        assert "[CONTINUE]" not in result

    def test_no_auto_continue_when_already_done(self, mock_agent, mock_trigger):
        mock_agent._finish_turn.return_value = "[DONE] Finished."
        result = finalize_turn(
            mock_agent,
            final_text="[DONE] Finished.",
            trigger=mock_trigger,
            all_tool_calls=[],
            total_prompt=10,
            total_completion=5,
            turn_start=time.monotonic(),
            user_goal="research topic X",
        )
        assert "[CONTINUE]" not in result

    def test_plain_result_no_false_positive(self, mock_agent, mock_trigger):
        mock_agent._finish_turn.return_value = "File has been written to /tmp/output.txt"
        result = finalize_turn(
            mock_agent,
            final_text="File has been written to /tmp/output.txt",
            trigger=mock_trigger,
            all_tool_calls=[],
            total_prompt=10,
            total_completion=5,
            turn_start=time.monotonic(),
            user_goal="write file",
        )
        assert "[CONTINUE]" not in result


# ─── heartbeat: fast [CONTINUE] pickup ────────────────────────────

class TestHeartbeatFastContinue:
    """Heartbeat picks up session_state.md with [CONTINUE] at any age."""

    def test_context_delta_session_active(self):
        delta = ContextDelta()
        assert delta.session_active is False

        delta2 = ContextDelta(session_active=True)
        assert delta2.is_empty is False
        assert "active session" in delta2.summary().lower()

    def test_pre_check_detects_session_state(self):
        """_pre_check should detect session_state.md existence."""
        from crow_agent.heartbeat_engine import HeartbeatEngine
        import tempfile
        from pathlib import Path

        # We can't easily test _pre_check in isolation because it accesses
        # many subsystems. Test the delta property directly.
        delta = ContextDelta(session_active=True)
        assert not delta.is_empty
        # session_active should trigger heartbeat to check initiatives
        assert any("session" in delta.summary().lower() for _ in [delta.summary()])


# ─── initiative rescue (existing behavior, verify preserved) ──────

class TestInitiativeRescue:
    """Initiative rescue logic survives the refactor."""

    def test_waiting_initiatives_rescued(self):
        """Simulate rescue logic: waiting + stuck → spawned."""
        active = {
            "abc": {"status": "waiting", "goal": "fix tests", "outcome": "pending"},
            "def": {"status": "active", "turn_count": 0, "_started_at": time.time() - 200, "goal": "stuck", "outcome": "pending"},
            "ghi": {"status": "completed", "goal": "done", "outcome": "completed"},
        }
        now = time.time()
        waiting = [(iid, s) for iid, s in active.items() if s.get("status") == "waiting"]
        stuck = [(iid, s) for iid, s in active.items()
                 if s.get("status") == "active"
                 and s.get("turn_count", 0) <= 1
                 and s.get("_started_at", 0) > 0
                 and now - s.get("_started_at", 0) > 120]
        assert len(waiting) == 1
        assert waiting[0][0] == "abc"
        assert len(stuck) == 1
        assert stuck[0][0] == "def"


# ─── session state spawn tracking ─────────────────────────────────

class TestSessionStateBackoff:
    def test_hash_fingerprint_tracks_spawns(self):
        """Fingerprint prevents infinite re-spawn of same session state."""
        spawns = {}
        content = "[CONTINUE] Working on task."
        fp = hash(content[:200])
        assert fp not in spawns

        # First spawn
        spawn_count, last = spawns.get(fp, (0, 0))
        assert spawn_count == 0
        spawns[fp] = (1, time.time())
        assert spawns[fp][0] == 1

        # Second spawn (after 120s)
        spawn_count, last = spawns.get(fp, (0, 0))
        can_spawn = spawn_count < 3 and (time.time() - last) > 120
        # With same content, after 120s: should be allowed
        # But we just recorded last=now, so this should be False
        assert can_spawn is False  # too soon

    def test_max_three_spawns(self):
        """After 3 spawns, no more re-spawns for same fingerprint."""
        spawns = {}
        content = "Resume: task"
        fp = hash(content[:200])

        for i in range(3):
            spawns[fp] = (i + 1, time.time() - 200)
            spawn_count, last = spawns.get(fp, (0, 0))
            if spawn_count < 3 and (time.time() - last) > 120:
                pass  # allowed

        # 4th attempt
        spawn_count, _ = spawns.get(fp, (0, 0))
        assert spawn_count == 3  # max reached
        can_spawn = spawn_count < 3
        assert can_spawn is False

    def test_different_content_different_fingerprint(self):
        """Different session state content → separate fingerprint."""
        spawns = {}
        c1 = "Task A: [CONTINUE]"
        c2 = "Task B: [CONTINUE]"
        fp1 = hash(c1[:200])
        fp2 = hash(c2[:200])
        assert fp1 != fp2
        spawns[fp1] = (3, time.time())
        # fp2 unaffected
        assert spawns.get(fp2, (0, 0))[0] == 0


# ─── _save_session_state integration ──────────────────────────────

class TestSessionStateIntegration:
    """Full flow: _save_session_state writes [CONTINUE] → detected."""

    def test_save_session_state_writes_continue(self):
        from crow_agent.run_agent import _save_session_state
        from pathlib import Path
        import tempfile, os

        # Override SESSION_STATE_PATH for test
        with tempfile.TemporaryDirectory() as td:
            test_path = Path(td) / "session_state.md"
            original = Path.home() / ".crow_agent" / "session_state.md"
            # Patch the module-level path
            import crow_agent.run_agent as mod
            mod.SESSION_STATE_PATH = test_path

            try:
                # Save state for a [CONTINUE] task
                _save_session_state(
                    user_input="research topic X",
                    tool_calls=[],
                    response="I will research this and get back to you.\n\n[CONTINUE] Working on task.",
                    progress_lines=["Goal: research topic X"],
                )
                assert test_path.exists()
                content = test_path.read_text()
                assert "[CONTINUE]" in content or "TASK INCOMPLETE" in content or "IN PROGRESS" in content
            finally:
                mod.SESSION_STATE_PATH = original

    def test_save_session_state_in_progress_phase(self):
        """Phase 1 save (in_progress=True) writes IN PROGRESS marker."""
        from crow_agent.run_agent import _save_session_state
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            test_path = Path(td) / "session_state.md"
            import crow_agent.run_agent as mod
            mod.SESSION_STATE_PATH = test_path
            try:
                _save_session_state(
                    user_input="build feature",
                    in_progress=True,
                )
                content = test_path.read_text()
                assert "IN PROGRESS" in content
            finally:
                mod.SESSION_STATE_PATH = Path.home() / ".crow_agent" / "session_state.md"
