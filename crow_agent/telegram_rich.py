"""Shared Telegram rich-message helpers.

Telegram HTML parse mode supported tags (Bot API 10.0):
  <b>/<strong>, <i>/<em>, <u>/<ins>, <s>/<strike>/<del>,
  <code>, <pre>, <a>, <blockquote>, <tg-spoiler>

NOT supported: <table>, <tr>, <th>, <td>, <ul>, <ol>, <li>
"""

from __future__ import annotations

import re

from telegram.helpers import escape


_FENCE_PLACEHOLDER = "\x00FENCE%d\x00"


def contains_pipe_table(text: str) -> bool:
    """True when text contains markdown-style pipe table rows."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and not re.match(r"^\|[\s\-:]+\|", stripped):
            return True
    return False


def _safe_html_chunks(html: str, max_len: int = 4000) -> list[str]:
    """Split HTML into chunks at tag boundaries, never mid-tag."""
    if len(html) <= max_len:
        return [html]
    chunks: list[str] = []
    pos = 0
    while pos < len(html):
        end = min(pos + max_len, len(html))
        if end == len(html):
            chunks.append(html[pos:])
            break
        safe = end
        idx_close = html.rfind("</", pos, end)
        if idx_close > pos:
            close = html.find(">", idx_close)
            if close != -1 and close < end:
                safe = close + 1
        else:
            idx_nl = html.rfind("\n", pos, end)
            if idx_nl > pos:
                safe = idx_nl + 1
        chunks.append(html[pos:safe])
        pos = safe
    return chunks


def _render_pipe_table(lines: list[str]) -> str:
    """Render markdown pipe-table rows as aligned <pre> block.

    Strips HTML tags from cells — Telegram doesn't render tags inside <pre>.
    """
    if not lines:
        return ""

    rows: list[list[str]] = []
    for line in lines:
        cells = [c.strip() for c in line.split("|")[1:-1]]
        # Strip inline HTML tags for clean monospace alignment
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        rows.append(cells)

    if not rows:
        return ""

    # Calculate column widths
    col_widths = [0] * max(len(r) for r in rows)
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    out_lines: list[str] = []
    for ri, row in enumerate(rows):
        padded = [cell.ljust(col_widths[i]) for i, cell in enumerate(row)]
        out_lines.append("  ".join(padded))
        if ri == 0 and len(rows) > 1:
            out_lines.append("  ".join("─" * w for w in col_widths))
    return f"<pre>{chr(10).join(out_lines)}</pre>"


def _format_pipe_tables(text: str) -> str:
    """Convert markdown pipe tables to Telegram <pre> blocks."""
    lines = text.split("\n")
    result: list[str] = []
    table_lines: list[str] = []
    in_table = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            if not in_table:
                in_table = True
                table_lines = []
            if re.match(r"^\|[\s\-:]+\|", stripped):
                continue
            table_lines.append(stripped)
        else:
            if in_table and table_lines:
                result.append(_render_pipe_table(table_lines))
                table_lines = []
                in_table = False
            result.append(line)

    if in_table and table_lines:
        result.append(_render_pipe_table(table_lines))

    return "\n".join(result)


def format_telegram_html(text: str, rich_tables: bool = True) -> str:
    """Convert markdown-like text to Telegram-safe HTML.

    Handles: fenced code blocks, inline code, bold, italic, links,
    bare URLs, lists, headings, blockquotes, spoilers, pipe tables.
    """
    fences: list[str] = []

    def _save_fence(m: re.Match) -> str:
        lang = m.group(1).strip() or ""
        code = m.group(2)
        if lang:
            fences.append(f'<pre><code class="language-{escape(lang)}">{escape(code)}</code></pre>')
        else:
            fences.append(f"<pre><code>{escape(code)}</code></pre>")
        return _FENCE_PLACEHOLDER % (len(fences) - 1)

    # 1. Extract fenced code blocks
    text = re.sub(r"```(\w*)\n(.*?)```", _save_fence, text, flags=re.DOTALL)

    # 2. Parse blockquotes BEFORE HTML escape (escape converts > to &gt;)
    lines = text.split("\n")
    result: list[str] = []
    in_blockquote = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">! "):
            if not in_blockquote:
                result.append("<blockquote expandable>")
                in_blockquote = True
            result.append(stripped[3:])
            continue
        elif stripped.startswith("> "):
            if not in_blockquote:
                result.append("<blockquote>")
                in_blockquote = True
            result.append(stripped[2:])
            continue
        elif in_blockquote:
            result.append("</blockquote>")
            in_blockquote = False
        result.append(line)
    if in_blockquote:
        result.append("</blockquote>")
    text = "\n".join(result)

    # 3. Escape HTML special chars
    text = escape(text)

    # 4. Headings and lists
    lines = text.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^#{1,3}\s+", stripped):
            heading = re.sub(r"^#{1,3}\s+", "", stripped)
            result.append(f"<b>{heading}</b>")
        else:
            list_match = re.match(r"^[-*]\s+(.+)", stripped)
            if list_match:
                result.append(f"• {list_match.group(1)}")
            else:
                result.append(line)
    text = "\n".join(result)

    # 5. Inline formatting (code first to protect inner content)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r'(?<!href=")(https?://[^\s<"\'\]\)]+)', r'<a href="\1">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![*/])\*(?![/*])(.+?)(?<![*/])\*(?![/*])", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"\|\|(.+?)\|", r"<tg-spoiler>\1</tg-spoiler>", text)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text)

    # 6. Restore fenced code blocks
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r'(?<!href=")(https?://[^\s<"\'\]\)]+)', r'<a href="\1">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![*/])\*(?![/*])(.+?)(?<![*/])\*(?![/*])", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", text)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text)

    # 5. Restore fenced code blocks
    for i, fence in enumerate(fences):
        text = text.replace(_FENCE_PLACEHOLDER % i, fence)

    # 6. Convert pipe tables to <pre> (last — cells already inline-formatted)
    text = _format_pipe_tables(text)
    return text
