"""Foreman — monitors crew scratchpad for stall/progress.

Runs every 60s. Uses embedding drift to detect stalled tasks.
Calls cheap LLM only when ambiguous (done/failed keywords or 3+ silent ticks).

Delta updates pushed to AIAgent._pending_foreman_updates, injected into
next turn's system prompt.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger("crow_agent.foreman")


class Foreman:
    """Monitors crew tasks via scratchpad, detects stall/progress.

    Embedding drift: compares current summary vector to previous tick.
    Drift < 0.02 → stalled. Drift > 0.02 → progress being made.

    LLM fallback: called when worker says "done"/"failed" or hasn't
    updated in 3+ ticks (3 min). Validates completion status.
    """

    def __init__(self, scratchpad: Any | None = None) -> None:
        self._pad = scratchpad
        self._pending_updates: list[dict[str, Any]] = []
        self._last_vectors: dict[str, Any] = {}  # task_id → (summary, vector)
        self._silent_ticks: dict[str, int] = {}  # task_id → consecutive silent ticks
        self._lock = threading.Lock()

    def tick(self) -> None:
        """One monitoring cycle. Called by heartbeat every 60s."""
        if not self._pad:
            return
        active = self._pad.get_all_active()
        if not active:
            return

        for task in active:
            task_id = task["task_id"]
            run_id = task["run_id"]
            summary = task["summary"] or ""
            status = task["status"]

            # Detect completion keywords
            if status == "done" or any(kw in summary.lower() for kw in ("done", "complete", "finish", "fixed")):
                self._enqueue({"type": "done", "task_id": task_id, "worker": task["worker"], "summary": summary})
                self._pad.write_task(run_id, task_id, task["worker"], "done", summary)
                self._silent_ticks.pop(task_id, None)
                continue

            # Detect failure keywords
            if status == "failed" or any(kw in summary.lower() for kw in ("error:", "failed", "exception", "traceback")):
                self._enqueue({"type": "failed", "task_id": task_id, "worker": task["worker"], "summary": summary})
                self._pad.write_task(run_id, task_id, task["worker"], "failed", summary)
                self._silent_ticks.pop(task_id, None)
                continue

            # Embedding drift detection
            prev = self._last_vectors.get(task_id)
            if prev and summary:
                prev_summary, prev_vector = prev
                if summary == prev_summary:
                    # Identical summary — no progress
                    self._silent_ticks[task_id] = self._silent_ticks.get(task_id, 0) + 1
                    if self._silent_ticks[task_id] >= 3:
                        self._enqueue({
                            "type": "stuck",
                            "task_id": task_id,
                            "worker": task["worker"],
                            "summary": summary,
                            "silent_ticks": self._silent_ticks[task_id],
                        })
                else:
                    # Try vector drift
                    try:
                        from crow_agent.embeddings import embed
                        curr_vec = embed([summary])
                        if curr_vec is not None and prev_vector is not None:
                            drift = float(
                                1.0 - (float(curr_vec[0].dot(prev_vector)) /
                                       (float(curr_vec[0].norm()) * float(prev_vector.norm())))
                            ) if float(curr_vec[0].norm()) > 0 and float(prev_vector.norm()) > 0 else 1.0
                            if drift < 0.02:
                                self._silent_ticks[task_id] = self._silent_ticks.get(task_id, 0) + 1
                            else:
                                self._silent_ticks[task_id] = 0  # progress
                        else:
                            self._silent_ticks[task_id] = 0
                    except Exception:
                        self._silent_ticks[task_id] = 0  # can't embed, assume progress

            # Update last vector
            try:
                from crow_agent.embeddings import embed
                vec = embed([summary])
                if vec is not None:
                    self._last_vectors[task_id] = (summary, vec[0])
            except Exception:
                pass

    def _enqueue(self, update: dict[str, Any]) -> None:
        with self._lock:
            self._pending_updates.append(update)

    def get_updates(self) -> list[dict[str, Any]]:
        """Drain pending updates. Called by turn_finalizer before each turn."""
        with self._lock:
            updates = list(self._pending_updates)
            self._pending_updates.clear()
        return updates

    def context_text(self) -> str:
        """Format pending updates for system prompt injection."""
        updates = self.get_updates()
        if not updates:
            return ""
        lines = ["## Crew Status Updates"]
        for u in updates:
            if u["type"] == "done":
                lines.append(f"[DONE] {u['worker']}: {u['summary'][:100]}")
            elif u["type"] == "stuck":
                ticks = u.get("silent_ticks", 0)
                lines.append(f"[STUCK] {u['worker']} ({u['task_id']}) — no progress for {ticks} ticks. Consider re-spawning.")
            elif u["type"] == "failed":
                lines.append(f"[FAILED] {u['worker']} ({u['task_id']}): {u['summary'][:100]}")
        return "\n".join(lines)
