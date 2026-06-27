"""Git tools: status and diff."""

from __future__ import annotations

from typing import Any


def register_tools(registry: Any, **kwargs: Any) -> None:
    """Register git tools."""

    @registry.register(
        description="Show working tree status (short format, like `git status --short`)"
    )
    def git_status() -> str:
        import subprocess
        r = subprocess.run("git status --short", shell=True, capture_output=True, text=True, timeout=15)
        return r.stdout.strip()

    @registry.register(
        description="Show unstaged diff (like `git diff`), optionally for a specific path"
    )
    def git_diff(path: str = ".") -> str:
        import subprocess
        r = subprocess.run(f"git diff -- {path}", shell=True, capture_output=True, text=True, timeout=15)
        return r.stdout.strip()
