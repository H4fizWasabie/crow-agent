"""Shared helpers for built-in tool modules."""

from __future__ import annotations

import ipaddress
import socket
import time
from pathlib import Path
from typing import Any


def is_private_host(host: str) -> bool:
    """Check if a hostname resolves to a private or internal address."""
    # Block by hostname pattern
    if host in ("localhost", "localhost.localdomain"):
        return True
    if host.endswith(".local") or host.endswith(".internal"):
        return True
    # Try DNS resolution for hostnames
    if not host.replace(".", "").isdigit():
        try:
            ips = socket.getaddrinfo(host, 80)
            for addr in ips:
                ip = addr[4][0]
                try:
                    if ipaddress.ip_address(ip).is_private:
                        return True
                except ValueError:
                    continue
        except (socket.gaierror, OSError):
            return False
        return False
    # Direct IP check
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def parse_page_spec(spec: str, total: int) -> list[int]:
    """Parse page spec like "1-3" or "1,3,5" into 0-based indices."""
    indices: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            start, end = int(start_str.strip()), int(end_str.strip())
            if start < 1 or end > total:
                raise ValueError(f"Pages {start}-{end} out of range (1-{total})")
            indices.update(range(start - 1, end))
        else:
            p = int(part)
            if p < 1 or p > total:
                raise ValueError(f"Page {p} out of range (1-{total})")
            indices.add(p - 1)
    return sorted(indices)


_last_vault_rebuild: float = 0.0

def rebuild_vault_index(wiki_dir: Path) -> None:
    """Scan wiki/pages/ and rewrite index.md wiki section. Debounced: max 1x/30s."""
    global _last_vault_rebuild
    now = time.monotonic()
    if now - _last_vault_rebuild < 30:
        return
    _last_vault_rebuild = now
    vault_root = wiki_dir.parent.parent
    ipath = vault_root / "index.md"
    pages = sorted(wiki_dir.glob("*.md"))
    parts = [
        "# Memory Vault — Index\n",
        f"**Last updated:** {__import__('datetime').date.today()}\n",
        "\n## Identity\n",
        "- [SOUL.md](SOUL.md)\n",
        "- [RULES.md](RULES.md)\n",
        "- [USER.md](USER.md)\n",
        "\n## Wiki Pages\n",
    ]
    for p in pages:
        title = p.stem.replace("-", " ").title()
        parts.append(f"- [{title}](wiki/pages/{p.name})\n")
    parts.append("\n## Log\n")
    parts.append("- [Changelog](log.md)\n")
    ipath.write_text("".join(parts), encoding="utf-8")
