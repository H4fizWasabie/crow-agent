"""Content-aware tool output compression.

Stores full output in SQLite, returns a structural summary with
key information and a retrieval ID. The LLM calls `retrieve(id)`
if it needs the original content.
"""

from __future__ import annotations

import json
import re
from functools import partial
from typing import Any, Callable

# Max chars for inline details in summary
MAX_DETAIL = 600
MAX_LINES = 5
_REPEAT_THRESHOLD = 3  # collapse N+ consecutive identical lines


def _collapse_repetition(output: str) -> str:
    """Collapse repeated lines to save context. Handles stack traces."""
    lines = output.splitlines()
    if len(lines) < 5:
        return output
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Count consecutive identical lines
        count = 1
        while i + count < len(lines) and lines[i + count] == line:
            count += 1
        if count >= _REPEAT_THRESHOLD:
            result.append(line)
            result.append(f"  [... {count - 1} identical lines collapsed]")
            i += count
        # Collapse stack trace frames (lines starting with File "...")
        elif line.strip().startswith('File "') and i + 3 < len(lines):
            frame_count = 1
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith('File "'):
                frame_count += 1
                j += 1
            if frame_count >= _REPEAT_THRESHOLD:
                result.append(line)
                result.append(f"  [... {frame_count} stack frames collapsed]")
                if j < len(lines):
                    result.append(lines[j])  # the actual error line
                    i = j + 1
                else:
                    i = j
            else:
                result.append(line)
                i += 1
        else:
            result.append(line)
            i += 1
    return "\n".join(result)


# Tool outputs above this size (chars) get compressed with a retrieval ID.
# Smaller outputs pass through inline — saves LLM round trips for everyday results.
COMPRESS_MIN_CHARS = 6000
_NO_COMPRESS_TOOLS: frozenset = frozenset({"read_file", "run_script"})
# ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
# read_file is exempt from compression on purpose:
# When read_file outputs WERE compressed, the LLM started chunking
# reads into 50-line snippets to avoid compression thresholds.
# This wasted more tokens than compression saved.
# Do NOT remove read_file from this set without testing for
# the perverse-incentive pattern first.
# See memory vault/wiki/pages/context-management.md for details.


# ── Summarizers (one per tool, all take output: str, oid: str) ──


def _summarize_cmd(output: str, oid: str) -> str:
    """Summarize shell command output."""
    lines = output.splitlines()
    n = len(lines)
    exit_code = _detect_exit_code(output)
    pytest_fails = _find_pytest_failures(output)
    error_lines = [l for l in lines if l.startswith("ERROR") or l.startswith("CRITICAL")]
    warn_lines = [l for l in lines if l.startswith("WARNING")]

    parts = [f"[output:{oid}] run_cmd → exit {exit_code}, {n} lines"]

    if pytest_fails:
        parts.append(f"  pytest: {pytest_fails}")
    if error_lines:
        parts.append(f"  errors: {len(error_lines)}")
        for el in error_lines[:3]:
            parts.append(f"    {el[:200]}")
    if warn_lines:
        parts.append(f"  warnings: {len(warn_lines)}")
    if not error_lines and not pytest_fails and n > 10:
        parts.append(f"  first: {lines[0][:200]}")
        if n > 1:
            parts.append(f"  last:  {lines[-1][:200]}")

    parts.append(f'  \u2192 Call retrieve("{oid}") for full output')
    return "\n".join(parts)


def _summarize_file(output: str, oid: str) -> str:
    """Summarize file read output."""
    lines = output.splitlines()
    n = len(lines)
    symbols = _find_symbols(lines)
    parts = [f"[output:{oid}] read_file \u2192 {n} lines"]

    if symbols:
        parts.append(f"  defines: {', '.join(symbols[:8])}")
        if len(symbols) > 8:
            parts[-1] += f" + {len(symbols)-8} more"

    preview = "\n".join(lines[:min(5, n)])
    if preview:
        parts.append(f"  preview:\n{preview[:300]}")

    parts.append(f'  \u2192 Call retrieve("{oid}") for full content')
    return "\n".join(parts)


def _summarize_grep(output: str, oid: str) -> str:
    """Summarize grep results."""
    lines = output.splitlines()
    n = len(lines)
    files = set()
    for l in lines:
        if ":" in l:
            files.add(l.split(":", 1)[0])
    parts = [f"[output:{oid}] grep \u2192 {len(lines)} matches in {len(files)} files"]

    for l in lines[:5]:
        parts.append(f"  {l[:200]}")
    if n > 5:
        parts.append(f"  ... {n-5} more matches")

    parts.append(f'  \u2192 Call retrieve("{oid}") for full results')
    return "\n".join(parts)


def _summarize_diff(output: str, oid: str) -> str:
    """Summarize git diff output."""
    lines = output.splitlines()
    n = len(lines)
    files_changed = [l for l in lines if l.startswith("diff --git")]
    insertions = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    deletions = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))

    parts = [f"[output:{oid}] git_diff \u2192 {len(files_changed)} files (+{insertions} -{deletions})"]
    for f in files_changed[:10]:
        parts.append(f"  {f.split()[-1]}")
    if len(files_changed) > 10:
        parts.append(f"  ... and {len(files_changed)-10} more files")

    hunk_lines = [l for l in lines if l.startswith("@@")]
    if hunk_lines:
        parts.append(f"  hunks: {hunk_lines[0]}")
        if len(hunk_lines) > 1:
            parts.append(f"         {hunk_lines[-1]}")

    parts.append(f'  \u2192 Call retrieve("{oid}") for full diff')
    return "\n".join(parts)


def _truncate(
    output: str, oid: str,
    prefix: str = "output", max_preview: int = MAX_DETAIL,
) -> str:
    """Generic truncate: first/last lines with size note."""
    lines = output.splitlines()
    parts = [f"[output:{oid}] {prefix} \u2192 {len(lines)} lines, {len(output)} chars"]
    if len(lines) <= MAX_LINES * 2:
        parts.append(output[:max_preview])
    else:
        for l in lines[:MAX_LINES]:
            parts.append(f"  {l[:200]}")
        parts.append(f"  ... ({len(lines) - MAX_LINES * 2} lines omitted)")
        for l in lines[-MAX_LINES:]:
            parts.append(f"  {l[:200]}")
    parts.append(f'  \u2192 Call retrieve("{oid}") for full output')
    return "\n".join(parts)


# ── summarizer registry ──
# Add new summarizers here instead of extending an if-elif chain.

_SUMMARIZERS: dict[str, Callable[[str, str], str]] = {
    "run_cmd": _summarize_cmd,
    "write_file": lambda o, oid: f"[output:{oid}] write_file \u2192 {len(o)} chars written",
    "edit_file": partial(_truncate, prefix="edit_file"),
    "grep_files": _summarize_grep,
    "list_dir": partial(_truncate, prefix="list_dir"),
    "git_status": partial(_truncate, prefix="git_status"),
    "git_diff": _summarize_diff,
    "spawn_agent": partial(_truncate, prefix="spawn_agent"),
    "spawn_team": partial(_truncate, prefix="spawn_team"),
    "convert_file": partial(_truncate, prefix="convert_file"),
    "get_time": lambda o, _oid: o,
}


def summarize_tool_output(
    tool_name: str,
    output: str,
    output_id: str,
) -> str:
    """Compress a tool's output into a short summary with retrieval ID.

    Returns a string like:
      [output:o_abcd1234] run_cmd → exit 0, 847 lines
      First: ... Last: ...
      → Call retrieve("o_abcd1234") for full output
    """
    if not output:
        return f"[output:{output_id}] (empty)"

    # Semantic dedup: collapse repetition before summarizing
    output = _collapse_repetition(output)

    fn = _SUMMARIZERS.get(tool_name)
    if fn:
        return fn(output, output_id)

    # Default: generic truncate
    return _truncate(output, output_id)


# ── Helpers ──


def _detect_exit_code(output: str) -> str:
    m = re.search(r"exit code: (\d+)", output)
    return m.group(1) if m else "0" if output else "?"


def _find_pytest_failures(output: str) -> str:
    lines = output.splitlines()
    fails = [l for l in lines if "FAILED" in l]
    if fails:
        seen = set()
        names_dedup = []
        for l in fails:
            m = re.search(r"FAILED\s+(\S+)", l)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                names_dedup.append(m.group(1))
        summary = f"{len(fails)} failures"
        if names_dedup:
            summary += ": " + ", ".join(names_dedup[:5])
            if len(names_dedup) > 5:
                summary += f" + {len(names_dedup)-5} more"
        return summary
    for l in lines[-5:]:
        m = re.search(r"(\d+)\s+failed", l)
        if m:
            return f"{m.group(1)} failures"
    return ""


def _find_symbols(lines: list[str]) -> list[str]:
    symbols = []
    for l in lines:
        l_stripped = l.strip()
        for kw in ("class ", "def ", "async def "):
            if l_stripped.startswith(kw):
                rest = l_stripped[len(kw):]
                name = rest.split("(")[0].split(":")[0].strip()
                if name and name not in symbols:
                    symbols.append(name)
                    if len(symbols) >= 12:
                        return symbols
    return symbols
