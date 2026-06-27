"""File safety system — backup, rollback, auto-verify, self-edit protection.

Deep module: tools call check_write_permission(path) / verify_after_edit(path)
and get safety guarantees without knowing the internals.

Rules:
  - Backup: copy file to .bak before overwriting
  - Rollback: restore from .bak if verify fails
  - Auto-verify: compile() Python files after edit
  - Self-edit: block writes to crow's own source
  - Sacred: block writes to .git, .env, credentials, /etc, /proc, /sys
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crow_agent.file_safety")

# ── Protection patterns ──

_SELF_EDIT_PATTERNS = [
    "crow_agent",
]

_SACRED_PATTERNS = [
    ".git/", ".gitconfig",
    ".env", ".env.",
    "/etc/", "/boot/", "/sys/", "/proc/",
    "credentials", "secrets", "password",
    "id_rsa", "id_ed25519",
]


def _is_self_edit(path: str | Path) -> bool:
    path_str = str(path)
    return any(p in path_str for p in _SELF_EDIT_PATTERNS)


def _is_sacred(path: str | Path) -> bool:
    path_str = str(path)
    return any(p in path_str for p in _SACRED_PATTERNS)


def check_write_permission(path: str | Path) -> tuple[bool, str]:
    """Check if Crow is allowed to write to this path. Returns (allowed, reason)."""
    p = Path(path).resolve()
    if _is_self_edit(p):
        return False, f"Blocked: self-edit protection — {p} is Crow's own source"
    if _is_sacred(p):
        return False, f"Blocked: sacred file protection — {p}"
    return True, ""


def backup_before_write(path: str | Path) -> Optional[Path]:
    """Create .bak backup before overwriting. Returns backup path or None."""
    p = Path(path)
    if not p.exists():
        return None
    bak = p.with_suffix(p.suffix + ".bak")
    try:
        shutil.copy2(str(p), str(bak))
        logger.debug("Backup: %s → %s", p, bak)
        return bak
    except OSError as e:
        logger.warning("Backup failed for %s: %s", p, e)
        return None


def rollback(path: str | Path) -> bool:
    """Restore file from .bak backup. Returns True if restored."""
    p = Path(path)
    bak = p.with_suffix(p.suffix + ".bak")
    if not bak.exists():
        return False
    try:
        shutil.copy2(str(bak), str(p))
        logger.info("Rollback: %s restored from backup", p)
        return True
    except OSError as e:
        logger.warning("Rollback failed for %s: %s", p, e)
        return False


def cleanup_backup(path: str | Path) -> None:
    """Remove the .bak backup file."""
    p = Path(path)
    bak = p.with_suffix(p.suffix + ".bak")
    if bak.exists():
        bak.unlink(missing_ok=True)


def verify_after_edit(path: str | Path) -> tuple[bool, str]:
    """Verify a file after editing. Compiles Python files for syntax check."""
    p = Path(path)
    if p.suffix != ".py":
        return True, ""
    try:
        compile(p.read_text(), str(p), "exec")
        return True, ""
    except SyntaxError as e:
        msg = f"Syntax error in {p}: {e}"
        logger.warning(msg)
        return False, msg


def safe_write(path: str | Path, content: str) -> tuple[bool, str]:
    """Write file with full safety: permission check → backup → write → verify → rollback."""
    p = Path(path).resolve()
    allowed, reason = check_write_permission(p)
    if not allowed:
        return False, reason
    bak = backup_before_write(p)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    except OSError as e:
        return False, f"Write failed: {e}"
    ok, verify_msg = verify_after_edit(p)
    if not ok:
        if bak and rollback(p):
            cleanup_backup(p)
            return False, f"{verify_msg} — rolled back"
        return False, verify_msg
    if bak:
        cleanup_backup(p)
    return True, f"Written: {p} ({len(content)} bytes)"


def safe_edit(path: str | Path, old_text: str, new_text: str) -> tuple[bool, str]:
    """Edit file with full safety: permission → backup → edit → verify → rollback."""
    p = Path(path).resolve()
    allowed, reason = check_write_permission(p)
    if not allowed:
        return False, reason
    if not p.exists():
        return False, f"File not found: {p}"
    bak = backup_before_write(p)
    try:
        content = p.read_text()
        if old_text not in content:
            return False, f"Text not found in {p}"
        new_content = content.replace(old_text, new_text, 1)
    except OSError as e:
        return False, f"Read failed: {e}"
    try:
        p.write_text(new_content)
    except OSError as e:
        return False, f"Write failed: {e}"
    ok, verify_msg = verify_after_edit(p)
    if not ok:
        if bak and rollback(p):
            cleanup_backup(p)
            return False, f"{verify_msg} — rolled back"
        return False, verify_msg
    if bak:
        cleanup_backup(p)
    return True, f"Edited: {p}"
