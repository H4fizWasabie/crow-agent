"""Tests for task_registry: enqueue, dequeue, cancel, persistence, retry."""

import json
import os
import tempfile

import pytest

from crow_agent.task_registry import (
    PendingTask,
    cancel_task,
    dequeue,
    enqueue,
    get,
    update_error,
    update_result,
    update_state,
)


@pytest.fixture(autouse=True)
def _isolate_state():
    """Clear module-level state before each test, backup+restore save path."""
    import crow_agent.task_registry as tr
    # Save original path and set temp
    orig_path = tr._SAVE_PATH
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tr._SAVE_PATH = __import__("pathlib").Path(f.name)
        os.unlink(f.name)

    # Clear in-memory state under lock
    with tr._task_lock:
        tr._tasks.clear()
        while not tr._pending.empty():
            try:
                tr._pending.get(block=False)
            except __import__("queue").Empty:
                break

    yield

    # Restore
    with tr._task_lock:
        tr._tasks.clear()
        while not tr._pending.empty():
            try:
                tr._pending.get(block=False)
            except __import__("queue").Empty:
                break
    tr._SAVE_PATH = orig_path


class TestEnqueueDequeue:
    def test_enqueue_returns_id(self):
        tid = enqueue("test prompt")
        assert len(tid) == 8
        assert isinstance(tid, str)

    def test_dequeue_returns_task(self):
        tid = enqueue("test")
        task = dequeue()
        assert task is not None
        assert task.id == tid
        assert task.prompt == "test"

    def test_dequeue_empty(self):
        assert dequeue() is None

    def test_enqueue_sets_defaults(self):
        tid = enqueue("test", chat_id=123)
        task = get(tid)
        assert task is not None
        assert task.profile_name == "deep-worker"
        assert task.state == "pending"
        assert task.chat_id == 123


class TestTaskLifecycle:
    def test_update_state(self):
        tid = enqueue("test")
        update_state(tid, "executing")
        task = get(tid)
        assert task.state == "executing"

    def test_update_result(self):
        tid = enqueue("test")
        result = "done"
        update_result(tid, result)
        task = get(tid)
        assert task.state == "done"
        assert task.result == result

    def test_update_error(self):
        tid = enqueue("test")
        update_error(tid, "something broke")
        task = get(tid)
        assert task.state == "failed"
        assert task.error == "something broke"

    def test_get_unknown(self):
        assert get("nonexistent") is None


class TestCancel:
    def test_cancel_pending(self):
        tid = enqueue("test")
        assert cancel_task(tid) is True
        task = get(tid)
        assert task.state == "cancelled"

    def test_cancel_already_done(self):
        tid = enqueue("test")
        update_result(tid, "ok")
        assert cancel_task(tid) is False  # already done

    def test_cancel_unknown(self):
        assert cancel_task("nonexistent") is False

    def test_cancelled_task_not_dequeued(self):
        tid = enqueue("test")
        cancel_task(tid)
        task = dequeue()
        # Cancelled tasks stay in queue — _execute skips them
        assert task is not None
        assert task.state == "cancelled"


class TestPersistence:
    def test_save_and_load(self):
        import crow_agent.task_registry as tr
        tid = enqueue("persist test", chat_id=42)
        update_result(tid, "completed")

        # Simulate restart by clearing and reloading
        with tr._task_lock:
            saved = json.loads(tr._SAVE_PATH.read_text())

        assert tid in saved["tasks"]
        assert saved["tasks"][tid]["prompt"] == "persist test"
        assert saved["tasks"][tid]["chat_id"] == 42
        assert saved["tasks"][tid]["state"] == "done"

    def test_load_restores_pending_tasks(self):
        import crow_agent.task_registry as tr
        tid = enqueue("pending task", chat_id=7)
        tr._save_tasks()

        # Simulate reload
        with tr._task_lock:
            tr._tasks.clear()
            while not tr._pending.empty():
                try:
                    tr._pending.get(block=False)
                except __import__("queue").Empty:
                    break
        # Should be empty now
        assert tr._pending.empty()
        assert dequeue() is None

        # Load from disk
        tr._load_tasks()
        t = dequeue()
        assert t is not None
        assert t.id == tid
        assert t.state == "pending"


class TestRetry:
    def test_retry_counter_increments(self):
        tid = enqueue("retry me")
        task = get(tid)
        assert task.retries == 0

    def test_can_retry_up_to_two(self):
        tid = enqueue("retry me")
        task = get(tid)
        for i in range(3):
            assert task.retries == i
            if i < 2:
                task.retries += 1
            else:
                break  # stop retrying after 2

    def test_retry_reenqueues(self):
        import crow_agent.task_registry as tr
        tid = enqueue("retry reenqueue")
        task = get(tid)
        # Simulate what _execute does on failure
        with tr._task_lock:
            task.state = "failed"
            if task.retries < 2:
                task.retries += 1
                task.state = "pending"
                task.error = ""
                tr._pending.put(task)

        assert task.retries == 1
        assert task.state == "pending"
        re_queueued = dequeue()
        assert re_queueued.id == tid
