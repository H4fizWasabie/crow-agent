"""Tests for HeartbeatEngine: ContextDelta, rate limiting, active tracking."""

import time

import crow_agent.heartbeat_engine as hb

ContextDelta = hb.ContextDelta


class TestContextDelta:
    def test_empty(self):
        d = ContextDelta()
        assert d.is_empty

    def test_not_empty_with_overdue(self):
        d = ContextDelta(overdue_tasks=["test"])
        assert not d.is_empty

    def test_not_empty_with_delegate(self):
        d = ContextDelta(delegate_pending=True)
        assert not d.is_empty

    def test_not_empty_with_cron_failures(self):
        d = ContextDelta(cron_failures=["malaysia_news"])
        assert not d.is_empty

    def test_not_empty_with_git_changes(self):
        d = ContextDelta(git_changes=" M file.py")
        assert not d.is_empty

    def test_not_empty_with_new_reports(self):
        d = ContextDelta(new_reports=["report_2026-06-16.md"])
        assert not d.is_empty

    def test_summary_empty(self):
        assert ContextDelta().summary() == ""

    def test_summary_contains_fields(self):
        d = ContextDelta(overdue_tasks=["task1"], delegate_pending=True)
        s = d.summary()
        assert "task1" in s
        assert "pending" in s


class TestActiveTurnTracking:
    def _reset(self):
        hb._heartbeat_active_turns = 0
        hb._heartbeat_last_user_interaction = 0.0

    def test_active_increments(self):
        self._reset()
        hb.mark_user_active()
        assert hb._heartbeat_active_turns == 1

    def test_active_decrements(self):
        self._reset()
        hb.mark_user_active()
        hb.mark_user_active()
        hb.mark_user_inactive()
        assert hb._heartbeat_active_turns == 1

    def test_inactive_never_negative(self):
        self._reset()
        hb.mark_user_inactive()
        hb.mark_user_inactive()
        hb.mark_user_inactive()
        assert hb._heartbeat_active_turns == 0

    def test_active_sets_timestamp(self):
        self._reset()
        before = time.time()
        hb.mark_user_active()
        assert hb._heartbeat_last_user_interaction >= before


class TestRateLimiting:
    def _make_engine(self):
        return hb.HeartbeatEngine()

    def test_can_act_initially(self):
        e = self._make_engine()
        assert e._can_act()

    def test_can_act_respects_max(self):
        e = self._make_engine()
        e._max_actions_per_hour = 2
        now = time.time()
        e._action_timestamps = [now - 60] * 2  # 2 actions within last hour
        assert not e._can_act()

    def test_can_act_under_max(self):
        e = self._make_engine()
        e._max_actions_per_hour = 3
        now = time.time()
        e._action_timestamps = [now - 60]  # 1 action under max of 3
        assert e._can_act()

    def test_can_act_old_actions_expire(self):
        e = self._make_engine()
        e._max_actions_per_hour = 1
        e._action_timestamps = [time.time() - 4000]  # older than 1 hour
        assert e._can_act()
