"""Process management tools: async exec, poll, log, kill for background processes.

Crow can start long-running processes and manage their lifecycle instead of
blocking the turn on a single run_cmd call.
"""

from __future__ import annotations

import os
import signal
import threading
import time
import uuid
from typing import Any


# ponytail: process registry is a plain dict, no DB persistence needed
_processes: dict[str, dict[str, Any]] = {}
_proc_lock = threading.Lock()


def _register(pid: int, cmd: str, cwd: str | None) -> str:
    """Register a process, return its ID."""
    proc_id = uuid.uuid4().hex[:8]
    with _proc_lock:
        _processes[proc_id] = {
            "id": proc_id,
            "pid": pid,
            "cmd": cmd[:200],
            "cwd": cwd or os.getcwd(),
            "started_at": time.time(),
            "done": False,
            "returncode": None,
            "log": [],
        }
    return proc_id


def register_tools(registry: Any, **kwargs: Any) -> None:
    """Register process management tools."""

    @registry.register(
        description="Run a shell command in the background. Returns immediately with a process ID. Use process_poll, process_log, process_kill to manage."
    )
    def exec_async(command: str, cwd: str = "", timeout: int = 3600) -> str:
        """Start a background process and return its ID immediately."""
        import subprocess
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd or None,
                preexec_fn=os.setsid,  # isolate process group for kill
            )
        except Exception as exc:
            return f"Error starting process: {exc}"

        proc_id = _register(proc.pid, command, cwd or None)

        # Background reader thread — collects stdout until process exits
        def _reader(pid: int, proc_id: str) -> None:
            try:
                for line in iter(proc.stdout.readline, b""):
                    with _proc_lock:
                        p = _processes.get(proc_id)
                        if p is None:
                            break
                        p["log"].append(line.decode("utf-8", errors="replace").rstrip())
                        # ponytail: cap log at 10K lines to prevent memory leak
                        if len(p["log"]) > 10000:
                            p["log"] = p["log"][-5000:]
            except Exception:
                pass
            finally:
                proc.wait()
                with _proc_lock:
                    p = _processes.get(proc_id)
                    if p:
                        p["done"] = True
                        p["returncode"] = proc.returncode

        t = threading.Thread(target=_reader, args=(proc.pid, proc_id), daemon=True)
        t.start()

        return f"Started process {proc_id} (PID {proc.pid}). Use process_poll, process_log, process_kill to manage."

    @registry.register(
        description="Poll a background process by ID and return its status (running/done/not found) and return code if done."
    )
    def process_poll(process_id: str) -> str:
        with _proc_lock:
            proc = _processes.get(process_id)
        if proc is None:
            return f"Process '{process_id}' not found."
        if not proc["done"]:
            # Check if still alive
            try:
                os.kill(proc["pid"], 0)  # sig 0 = test existence
                return f"Running (PID {proc['pid']}). Started {time.time() - proc['started_at']:.0f}s ago."
            except OSError:
                proc["done"] = True
                proc["returncode"] = -1  # dead but reader hasn't caught up

        status = "Done" if proc["returncode"] == 0 else f"Failed (exit {proc['returncode']})"
        return f"{status}. PID {proc['pid']}. Ran {time.time() - proc['started_at']:.0f}s. Log lines: {len(proc['log'])}."

    @registry.register(
        description="Get the full output log of a background process by ID. Use offset to paginate."
    )
    def process_log(process_id: str, offset: int = 0, limit: int = 100) -> str:
        with _proc_lock:
            proc = _processes.get(process_id)
        if proc is None:
            return f"Process '{process_id}' not found."
        lines = proc["log"][offset : offset + limit]
        if not lines:
            return f"(empty — offset {offset} beyond {len(proc['log'])} lines)"
        result = "\n".join(lines)
        total = len(proc["log"])
        return f"[{offset+1}-{offset+len(lines)}/{total}]\n{result}"

    @registry.register(
        description="Kill a background process by ID. Sends SIGTERM first, then SIGKILL after 3s if still alive."
    )
    def process_kill(process_id: str) -> str:
        with _proc_lock:
            proc = _processes.get(process_id)
        if proc is None:
            return f"Process '{process_id}' not found."
        if proc["done"]:
            return f"Process {process_id} already finished (exit {proc['returncode']})."
        pid = proc["pid"]
        try:
            # Send SIGTERM to the whole process group
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            return f"Process {process_id} (PID {pid}) already exited."
        except Exception as exc:
            return f"Kill error: {exc}"

        # Wait 3s for graceful exit, then SIGKILL
        time.sleep(3)
        try:
            os.kill(pid, 0)  # still alive?
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return f"Killed {process_id} (PID {pid}) with SIGKILL after SIGTERM."
        except ProcessLookupError:
            return f"Killed {process_id} (PID {pid}) with SIGTERM."

    @registry.register(
        description="List all active background processes with their IDs, commands, and status."
    )
    def process_list() -> str:
        with _proc_lock:
            if not _processes:
                return "No background processes."
            lines = []
            for proc_id, proc in sorted(_processes.items()):
                alive = False
                if not proc["done"]:
                    try:
                        os.kill(proc["pid"], 0)
                        alive = True
                    except OSError:
                        proc["done"] = True
                status = "RUNNING" if alive else f"EXIT {proc['returncode']}"
                age = time.time() - proc["started_at"]
                lines.append(f"{proc_id} {status} [{age:.0f}s] {proc['cmd'][:80]}")
            return "\n".join(lines)
