"""Tool executor — execute batch tool calls with parallel execution, error
classification, and output compression. Extracted from run_agent.py.

Interface: execute_tool_calls(tool_calls, messages, tools, db, session_id) → results
"""

from __future__ import annotations

import concurrent.futures
import json as _json
import re
from collections.abc import Callable
from typing import Any

from .output_compressor import COMPRESS_MIN_CHARS, _NO_COMPRESS_TOOLS, summarize_tool_output
from .providers import ChatMessage

# Module-level thread pool — reused across calls (Ren parity)
_TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)


# ── Option H: Read-output compression helpers ──

# Commands that are read-only (safe to compress)
_READ_COMMAND_PREFIXES = (
    "grep ", "fgrep ", "rg ", "find ", "cat ", "ls ", "head ", "tail ",
    "wc ", "du ", "df ", "stat ", "file ", "git log", "git show", "git diff",
    "git status", "git branch", "git tag", "git remote", "git config",
    "echo ", "printf ", "date", "whoami", "hostname", "pwd", "which ",
    "type ", "uname", "env", "printenv",
)

# Tools that are always read-type (output safe to aggressively compress)
_ALWAYS_READ_TOOLS = frozenset({
    "read_file", "grep_files", "web_search", "web_fetch",
    "session_search", "git_log", "git_diff", "git_status",
    "list_dir", "get_time",
})

# Tools that are always write-type (never compress output)
_ALWAYS_WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "run_script", "send_telegram",
    "spawn_agent", "spawn_team", "delegate_task", "create_skill",
    "generate_image",
})


def _is_read_command(cmd_str: str) -> bool:
    """Classify a shell command as read-only or not.

    Uses prefix matching on the command string. Shell pipelines are
    classified by the first segment only.
    """
    stripped = cmd_str.strip()
    # Remove harmless stderr redirects before checking
    stripped = re.sub(r'2>/dev/null', '', stripped)
    stripped = re.sub(r'2>&1', '', stripped)
    stripped = stripped.strip()
    # Output redirection to a real target → write operation
    if re.search(r'>', stripped):
        return False
    # Strip common cd prefixes (substring, not character set)
    for cd_prefix in ("cd /opt/crow-agent && ", "cd /opt/crow-agent; ",
                       "cd /some/path && ", "cd /some/path; ",
                       "cd /tmp && ", "cd /tmp; "):
        if stripped.startswith(cd_prefix):
            stripped = stripped[len(cd_prefix):]
            break
    for prefix in _READ_COMMAND_PREFIXES:
        if stripped.startswith(prefix):
            return True
    return False


def _is_read_tool(name: str, args_str: str) -> bool:
    """Classify a tool call as read-type.

    Returns True if the tool's output should be aggressively compressed.
    read_file, grep_files, web_search, web_fetch are always read.
    run_cmd is delegated to _is_read_command(value of "command" arg).
    Unknown tools default to NOT read (safe default — don't compress writes).
    """
    if name in _ALWAYS_READ_TOOLS:
        return True
    if name in _ALWAYS_WRITE_TOOLS:
        return False
    if name == "run_cmd":
        try:
            args = _json.loads(args_str) if isinstance(args_str, str) else args_str
            cmd = args.get("command", "")
            return _is_read_command(cmd)
        except Exception:
            return False
    return False


def _compress_read_output(name: str, output: str) -> str:
    """Aggressively compress read-type tool output to prevent investigation spirals.

    Keeps first 5 lines + last 5 lines. Inserts [compressed N lines] marker.
    Short outputs (<10 lines) pass through unchanged.
    """
    if not output or not output.strip():
        return output
    lines = output.splitlines()
    if len(lines) <= 10:
        return output
    head = lines[:5]
    tail = lines[-5:]
    skipped = len(lines) - 10
    return "\n".join(head) + f"\n[... compressed {skipped} lines ...]\n" + "\n".join(tail)


def execute_tool_calls(
    tool_calls: list[dict[str, Any]],
    messages: list[ChatMessage],
    tools: Any,  # ToolRegistry
    db: Any,      # CrowState
    session_id: str,
    *,
    turn_id: int | None = None,
    on_tool: Callable[[str, str, str | None], None] | None = None,
    execute_one: Callable[[dict, Any, list[ChatMessage]], tuple[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], int, str, bool]:
    """Execute a batch of tool calls — parallel when possible.

    Appends ChatMessage(role="tool") to messages for each call.
    Compresses large outputs via DB storage + summarizer.
    Returns (all_tool_calls, consecutive_failures, last_error, should_abort).

    execute_one: override for testing. Default uses run_agent.execute_tool_call.
    """
    if execute_one is None:
        from .run_agent import execute_tool_call as _default_exec
        execute_one = _default_exec

    all_tool_calls: list[dict[str, Any]] = []
    consecutive_failures = 0
    last_error = ""

    # Single tool call — execute inline, no thread overhead
    if len(tool_calls) == 1:
        tc = tool_calls[0]
        all_tool_calls.append(tc)
        name = tc.get("function", {}).get("name", "")
        args_str = tc.get("function", {}).get("arguments", "")
        if on_tool:
            on_tool(name, "start", args_str)
        exec_name, result_str = execute_one(tc, tools, messages)
        if on_tool:
            on_tool(exec_name, "end", result_str[:200] if result_str else None)
        if _is_failure(result_str):
            consecutive_failures += 1
            last_error = result_str
        result_str = _compress(exec_name, result_str, args_str, db, session_id, turn_id)
        wrapped = f"---TOOL OUTPUT ({exec_name})---\n{result_str}\n---END TOOL OUTPUT ({exec_name})---"
        messages[-1] = ChatMessage(
            role="tool", content=wrapped, tool_call_id=tc["id"],
        )
        return all_tool_calls, consecutive_failures, last_error, False

    # Multi-tool — execute in parallel, collect results, process in order
    # ponytail: 4 workers matches existing ThreadPoolExecutor size
    results_by_id: dict[str, tuple[str, str]] = {}

    def _exec_one(tc: dict) -> tuple[str, str, str]:
        fn = tc["function"]
        t_name = fn["name"]
        if on_tool:
            on_tool(t_name, "start", str(fn.get("arguments", ""))[:200])
        exec_name, exec_result = execute_one(tc, tools, [])
        if on_tool:
            on_tool(exec_name, "end", exec_result[:200] if exec_result else None)
        return tc.get("id", ""), exec_name, exec_result

    futures_list = {_TOOL_EXECUTOR.submit(_exec_one, tc): tc for tc in tool_calls}
    for future in concurrent.futures.as_completed(futures_list):
        tc_id, exec_name, exec_result = future.result()
        results_by_id[tc_id] = (exec_name, exec_result)

    # Process in original order — compress, track failures, append to messages
    with db.batch():
        must_abort = False
        for tc in tool_calls:
            tc_id = tc.get("id", "")
            name, result_str = results_by_id.get(tc_id, ("?", "[TRANSIENT] Tool execution lost"))
            all_tool_calls.append(tc)

            if _is_failure(result_str):
                consecutive_failures += 1
                last_error = result_str
            else:
                consecutive_failures = 0

            result_str = _compress(
                name, result_str,
                tc.get("function", {}).get("arguments", ""),
                db, session_id, turn_id,
            )

            wrapped = f"---TOOL OUTPUT ({name})---\n{result_str}\n---END TOOL OUTPUT ({name})---"
            messages.append(ChatMessage(
                role="tool", content=wrapped, tool_call_id=tc_id,
            ))

            if consecutive_failures >= 3:
                must_abort = True

        if must_abort:
            return all_tool_calls, consecutive_failures, last_error, True

    return all_tool_calls, consecutive_failures, last_error, False


def _is_failure(result: str) -> bool:
    """Classify tool result as failure."""
    return "Error executing" in result or result.startswith("[SYSTEM]")


def _classify_error(result: str) -> str:
    """Classify error for adaptive recovery hints.

    Returns one of: timeout, missing_file, permission, auth, rate_limit,
    not_found, syntax, network, unknown.
    """
    r = result.lower()
    if "timeout" in r or "timed out" in r:
        return "timeout"
    if "modulenotfounderror" in r or "importerror" in r:
        return "missing_dep"
    if "filenotfounderror" in r or "no such file" in r or "enoent" in r:
        return "missing_file"
    if "permission denied" in r or "eacces" in r or "permissionerror" in r:
        return "permission"
    if "401" in r or "403" in r or "unauthorized" in r or "forbidden" in r:
        return "auth"
    if "429" in r or "rate limit" in r or "too many requests" in r:
        return "rate_limit"
    if "syntaxerror" in r or "indentationerror" in r or "nameerror" in r:
        return "syntax"
    if "connection" in r or "unreachable" in r or "refused" in r:
        return "network"
    if "not found" in r or "does not exist" in r:
        return "not_found"
    return "unknown"


# Adaptive recovery hints — injected at 2 consecutive failures (one before abort)
_ERROR_HINTS: dict[str, str] = {
    "timeout":         "Tool timed out. Try with smaller scope, fewer files, or shorter timeout.",
    "missing_dep":     "Module not installed. Use pip_install to add the missing package.",
    "missing_file":    "File not found. Check the path with list_dir or read_file.",
    "permission":      "Permission denied. Try a different path or check file ownership.",
    "auth":            "Authentication failed. Check API keys or token expiry.",
    "rate_limit":      "Rate limited. Wait a moment or switch to a different provider.",
    "syntax":          "Syntax error. Check the code for typos, missing imports, or indentation.",
    "network":         "Network error. Check connectivity or try again in a moment.",
    "not_found":       "Resource not found. Verify the resource exists and the path is correct.",
    "unknown":         "Tool failed. Try a different tool or approach to accomplish the goal.",
}


def get_error_hint(result: str) -> str:
    """Return an adaptive hint for the given error."""
    category = _classify_error(result)
    return _ERROR_HINTS.get(category, _ERROR_HINTS["unknown"])


def _compress(
    name: str,
    result: str,
    args_str: str,
    db: Any,
    session_id: str,
    turn_id: int | None = None,
) -> str:
    """Compress large tool output via DB storage + summarizer.
    Always stores send_telegram outputs for audit trail."""
    if name == "send_telegram":
        # ponytail: always log for audit — bypass COMPRESS_MIN_CHARS
        db.store_tool_output(session_id, name, result, arguments=args_str, turn_id=turn_id)
        return result
    if name not in _NO_COMPRESS_TOOLS and len(result) > COMPRESS_MIN_CHARS:
        oid = db.store_tool_output(session_id, name, result, arguments=args_str, turn_id=turn_id)
        return summarize_tool_output(name, result, oid)
    return result
