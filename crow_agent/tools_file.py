"""File operation tools: read, write, edit, list, grep, convert, restore."""

from __future__ import annotations

MAX_TOOL_OUTPUT_CHARS = 20000  # ponytail: hard cap to prevent context bloat

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

_BACKUP_DIR = Path.home() / ".crow_agent" / "backups"
_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# ponytail: guardrails for self-modification — allow it, but make it recoverable
_SELF_CODE_PATHS = frozenset({"crow_agent", "app.py", "templates", "tests"})
_SACRED_FILES = frozenset({".env", "providers.json", "auth.json", ".crow_agent"})
# Gradual commitment: multi-file edit plan tracking
_plan_savepoint: str | None = None  # git commit hash before plan started


def _is_self_edit(path: str) -> bool:
    """Check if path is Crow's own source code."""
    p = Path(path)
    parts = p.parts
    for prefix in _SELF_CODE_PATHS:
        if prefix in parts or p.name == prefix:
            return True
    return False


def _is_sacred(path: str) -> bool:
    """Check if path is a critical config file that must never be modified."""
    name = Path(path).name
    return name in _SACRED_FILES or ".crow_agent" in str(path)


def _auto_verify_if_code(self, path: str, result: str) -> str:
    """Auto-verify: if file is Python, run tests. Pass → commit, fail → rollback."""
    if not path.endswith(".py"):
        return result

    # Sacred file check — never auto-modify core config
    if _is_sacred(path):
        return result + " [tests skipped: sacred file]"

    import subprocess
    # Run test suite — quick exit on first failure
    r = subprocess.run(
        ["python", "-m", "pytest", "tests/", "-x", "-q"],
        capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT),
    )
    if r.returncode == 0:
        # Commit on success
        try:
            subprocess.run(
                ["git", "add", path],
                capture_output=True, text=True, timeout=10,
                cwd=str(PROJECT_ROOT),
            )
            subprocess.run(
                ["git", "commit", "-m", f"auto-verify: tests pass after edit {Path(path).name}"],
                capture_output=True, text=True, timeout=10,
                cwd=str(PROJECT_ROOT),
            )
            # ponytail: tag as verified so Crow won't revert it later
            subprocess.run(
                ["git", "tag", "-f", "verified-latest"],
                capture_output=True, text=True, timeout=5,
                cwd=str(PROJECT_ROOT),
            )
            return result + " ✅ Tests pass. Auto-committed + tagged verified."
        except Exception:
            return result + " ✅ Tests pass. (commit failed)"
    else:
        # Rollback on failure
        try:
            subprocess.run(
                ["git", "checkout", "--", path],
                capture_output=True, text=True, timeout=10,
                cwd=str(PROJECT_ROOT),
            )
        except Exception:
            pass
        # Include test failure summary
        last_lines = r.stdout.strip().split("\n")[-5:]
        return (
            result + f"\n⚠️ Tests FAILED. Changes rolled back.\n"
            f"```\n" + "\n".join(last_lines) + "\n```"
        )


def _git_savepoint(path: str) -> str:
    """Commit current state before self-edit. Returns commit hash or empty string."""
    try:
        r = subprocess.run(
            ["git", "add", path],
            capture_output=True, text=True, timeout=10,
        )
        r = subprocess.run(
            ["git", "commit", "-m", f"savepoint: pre-edit {Path(path).name}"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()[:8]
    except Exception:
        pass
    return ""


def _touch_trigger() -> None:
    """Touch trigger file to signal heartbeat: code changed, check now."""
    try:
        trigger = Path.home() / ".crow_agent" / "trigger_check"
        trigger.parent.mkdir(parents=True, exist_ok=True)
        trigger.touch()
    except OSError:
        pass


def _verify_after_edit(path: str) -> str | None:
    """Run tests after self-edit. Returns error message or None if ok."""
    p = Path(path)
    # Syntax check for .py files
    if p.suffix == ".py":
        r = subprocess.run(
            ["python3", "-m", "py_compile", path],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return f"Syntax error: {r.stderr.strip()[-200:]}"
    # Find matching test file
    test_name = f"test_{p.stem}.py"
    test_path = Path("tests") / test_name
    if not test_path.exists():
        return None  # No test file — skip verification
    r = subprocess.run(
        ["python3", "-m", "pytest", str(test_path), "-x", "--tb=line", "-q"],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return f"Tests failed for {test_name}: {r.stdout.strip()[-200:]}"
    return None


def _rollback_edit(path: str) -> str:
    """Roll back a self-edit using git. If a plan is active, resets ALL plan files."""
    global _plan_savepoint
    if _plan_savepoint:
        sp = _plan_savepoint
        try:
            subprocess.run(
                ["git", "reset", "--hard", sp],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "clean", "-fd", "crow_agent/"],
                capture_output=True, timeout=10,
            )
            _plan_savepoint = None
            return f"Plan aborted. All files restored to savepoint {sp}."
        except Exception as exc:
            return f"Plan rollback failed: {exc}"
    # Single-file rollback
    p = Path(path)
    try:
        r = subprocess.run(
            ["git", "ls-files", "--error-unmatch", path],
            capture_output=True, timeout=5,
        )
        if r.returncode == 0:
            subprocess.run(
                ["git", "checkout", "--", path],
                capture_output=True, timeout=10,
            )
            return f"Restored {path} from git"
        else:
            p.unlink(missing_ok=True)
            return f"Deleted new file {path}"
    except Exception as exc:
        return f"Rollback failed: {exc}"


def _backup_before_write(path: str) -> None:
    """Copy file to backups/ before modification. No-op if file doesn't exist."""
    src = Path(path)
    if not src.exists():
        return
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_name = src.name.replace("/", "_")
    dest = _BACKUP_DIR / f"{src.parent.name}_{safe_name}_{ts}.bak"
    try:
        shutil.copy2(src, dest)
    except OSError:
        pass  # best-effort, don't block the write


def register_tools(registry: Any, **kwargs: Any) -> None:
    """Register file operation tools."""

    @registry.register(description="Read file. Default 100 lines (use limit=500 for more, offset for position)")
    def read_file(path: str, limit: int = 100, offset: int = 0) -> str:
        # ponytail: default 100 lines (was 2000) — prevents full-file context overload
        # Crow reads entire files → gets distracted → chain-greps references → 69-tool spirals
        try:
            text = Path(path).read_text(encoding="utf-8")
            lines = text.splitlines()
            if offset > 0:
                lines = lines[offset:]
            if len(lines) > limit:
                lines = lines[:limit]
                lines.append(f"... truncated at {limit} lines (offset {offset})")
            return _cap_output("\n".join(lines))
        except FileNotFoundError:
            return f"File not found: {path}"
        except Exception as exc:
            return f"Error reading {path}: {exc}"

    @registry.register(description="Write content to a file, creating it if needed")
    def write_file(path: str, content: str) -> str:
        # Block sacred files
        if _is_sacred(path):
            return f"[PERMANENT] Cannot modify '{path}' — this is a critical config file."

        _backup_before_write(path)

        # Git savepoint before self-edit (non-blocking)
        savepoint = ""
        if _is_self_edit(path):
            savepoint = _git_savepoint(path)

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

        # Auto-rollback: verify self-edits don't break tests
        if _is_self_edit(path):
            verify_err = _verify_after_edit(path)
            if verify_err:
                rollback_msg = _rollback_edit(path)
                return f"[AUTO-ROLLBACK] Edit reverted. {verify_err}\n{rollback_msg}"

        result = f"Wrote {len(content)} chars to {path}"
        if savepoint:
            result += f" (savepoint: {savepoint})"
        # Event-driven: signal heartbeat to run immediate code check
        if _is_self_edit(path):
            _touch_trigger()
        return result

    @registry.register(
        description="Surgically replace text in a file (old_string must be unique)"
    )
    def edit_file(path: str, old_string: str, new_string: str) -> str:
        # Block sacred files
        if _is_sacred(path):
            return f"[PERMANENT] Cannot modify '{path}' — this is a critical config file."

        # Git savepoint before self-edit (non-blocking)
        savepoint = ""
        if _is_self_edit(path):
            savepoint = _git_savepoint(path)

        try:
            _backup_before_write(path)
            p = Path(path)
            text = p.read_text(encoding="utf-8")
            count = text.count(old_string)
            if count == 0:
                return f"Error: string not found in {path}"
            if count > 1:
                return f"Error: string appears {count} times in {path} — provide more context"
            text = text.replace(old_string, new_string)
            p.write_text(text, encoding="utf-8")

            # Auto-rollback: verify self-edits don't break tests
            if _is_self_edit(path):
                verify_err = _verify_after_edit(path)
                if verify_err:
                    rollback_msg = _rollback_edit(path)
                    return f"[AUTO-ROLLBACK] Edit reverted. {verify_err}\n{rollback_msg}"

            result = f"Replaced 1 occurrence in {path}"
            if savepoint:
                result += f" (savepoint: {savepoint})"
            # Event-driven: signal heartbeat to run immediate code check
            if _is_self_edit(path):
                _touch_trigger()
            return result
        except FileNotFoundError:
            return f"File not found: {path}"
        except Exception as exc:
            return f"Error editing {path}: {exc}"

    @registry.register(
        description="Begin a multi-edit plan. Saves git state. If any edit fails, ALL plan files roll back. Call commit_plan() when done."
    )
    def begin_plan() -> str:
        global _plan_savepoint
        try:
            r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                _plan_savepoint = r.stdout.strip()[:8]
                return f"Plan started at savepoint {_plan_savepoint}. Edits roll back together if any fails."
            return "Error: could not get current git commit"
        except Exception as exc:
            return f"Plan start failed: {exc}"

    @registry.register(
        description="Commit a multi-edit plan. Clears rollback — all edits are now permanent."
    )
    def commit_plan() -> str:
        global _plan_savepoint
        if _plan_savepoint:
            sp = _plan_savepoint
            _plan_savepoint = None
            return f"Plan committed. Savepoint {sp} cleared."
        return "No active plan to commit."

    @registry.register(
        description="Restore the most recent backup of a file. Provide path to get the latest backup."
    )
    def restore_file(path: str) -> str:
        """List and restore the most recent backup for a given file path."""
        p = Path(path)
        safe_name = p.name.replace("/", "_")
        parent_name = p.parent.name
        prefix = f"{parent_name}_{safe_name}_"
        candidates = sorted(_BACKUP_DIR.glob(f"{prefix}*.bak"))
        if not candidates:
            available = list(_BACKUP_DIR.iterdir())[:10]
            if available:
                hints = "\n".join(f"  {b.name}" for b in available)
                return f"No backup found for '{path}'. Available backups:\n{hints}"
            return f"No backup found for '{path}'. Backup directory is empty."
        latest = candidates[-1]
        shutil.copy2(latest, p)
        return f"Restored {path} from backup {latest.name}"

    @registry.register(description="List directory contents. Quick lookup only — for recursive search, use run_script or run_cmd with find.")
    def list_dir(path: str = ".", pattern: str = "*") -> str:
        try:
            entries = sorted(Path(path).glob(pattern))
            lines = []
            for e in entries:
                prefix = "[DIR]" if e.is_dir() else "[FILE]"
                lines.append(f"{prefix} {e.name}")
            return _cap_output("\n".join(lines)) if lines else f"Empty: {path}"
        except Exception as exc:
            return f"Error listing {path}: {exc}"

    @registry.register(description="Quick single-pattern text search. Returns match+context lines. For multi-file hunting or analysis, use run_script instead (one script = 10 grep+read calls).")
    def grep_files(pattern: str, path: str = ".", file_pattern: str = "*", max_results: int = 20, context_lines: int = 2) -> str:
        """Grep with context. Prefer run_script for bulk operations. This is for quick lookups only."""
        import subprocess
        try:
            cmd = ["grep", "-rnHIiF", pattern, path, "--include", file_pattern]
            # ponytail: pre-fetch context to avoid read_file follow-ups
            if context_lines > 0:
                cmd.insert(1, f"-C{context_lines}")
                cmd.insert(1, "--color=never")
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode > 1:
                return f"Grep error: {result.stderr.strip()}"
            lines = result.stdout.splitlines()
            if not lines:
                return f"No matches for '{pattern}'"
            if len(lines) > max_results:
                lines = lines[:max_results]
                lines.append(f"... capped at {max_results} results")
            return _cap_output("\n".join(lines))
        except subprocess.TimeoutExpired:
            return "Grep timed out"
        except Exception as exc:
            return f"Error: {exc}"

    @registry.register(
        description="Extract text from a PDF using PyMuPDF (faster than markitdown). Returns all text. Use first_page_only=true for preview."
    )
    def extract_pdf_text(path: str, first_page_only: bool = False) -> str:
        if not Path(path).exists():
            return f"File not found: {path}"
        try:
            import fitz
        except ImportError:
            return "PyMuPDF not installed. Run: pip install pymupdf"
        try:
            doc = fitz.open(path)
            pages = doc[:1] if first_page_only else doc
            lines = []
            for page in pages:
                lines.append(page.get_text())
            doc.close()
            text = "\n".join(lines)
            if not text.strip():
                text = "[No extractable text — may be a scanned/image PDF]"
            if len(text) > 50000:
                text = text[:50000] + f"\n\n... truncated at 50000 chars"
            return text
        except Exception as exc:
            return f"Error extracting PDF text: {exc}"

    @registry.register(
        description="Convert a file (PDF, Excel, Word, PPT, HTML, CSV, etc.) to Markdown text"
    )
    def convert_file(path: str) -> str:
        try:
            from markitdown import MarkItDown
        except ImportError:
            return "markitdown not installed. Run: pip install 'crow-agent[markitdown]'"
        try:
            md = MarkItDown()
            result = md.convert(path)
            text = result.text_content
            if not text:
                return f"File converted but produced no content: {path}"
            if len(text) > 50000:
                text = text[:50000] + f"\n\n... truncated at 50000 chars"
            return text
        except Exception as exc:
            return f"Error converting {path}: {exc}"


def _cap_output(result: str) -> str:
    """Hard cap tool output at MAX_TOOL_OUTPUT_CHARS to prevent context bloat."""
    if len(result) > MAX_TOOL_OUTPUT_CHARS:
        return result[:MAX_TOOL_OUTPUT_CHARS] + f"\n[...truncated {len(result) - MAX_TOOL_OUTPUT_CHARS} chars]"
    return result
