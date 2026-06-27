"""Task management tools: secretary integration."""

from __future__ import annotations

import os
from typing import Any

_price_db_conn: str | None = None


def register_tools(registry: Any, **kwargs: Any) -> None:
    """Register task management tools."""
    task_db = kwargs.get("task_db")

    @registry.register(description="Create a new task for the secretary. Use when the user mentions something they need to do.")
    def create_task(title: str, deadline: str = "", priority: str = "medium", description: str = "", repeat: str = "") -> str:
        from .crow_state import CrowState
        db = task_db or CrowState()
        if not deadline:
            deadline = None
        repeat_val = repeat if repeat else None
        tags = ["user-created"]
        task_id = db.create_task(
            title=title, description=description, deadline=deadline,
            priority=priority, tags=tags, repeat=repeat_val,
        )
        repeat_msg = f" 🔁 {repeat}" if repeat_val else ""
        return f"✅ Task created: {title}{repeat_msg} (id: {task_id})"

    @registry.register(description="List all tasks, optionally filtered by status (pending/in_progress/done/cancelled) or tag")
    def list_tasks(status: str = "", tag: str = "") -> str:
        from .crow_state import CrowState
        db = task_db or CrowState()
        tasks = db.list_tasks(status=status or None, tag=tag or None)
        if not tasks:
            return "No tasks found."
        lines = [f"**{len(tasks)} task(s):**"]
        for t in tasks:
            tag_str = f" [{','.join(t['tags'])}]" if t.get("tags") else ""
            deadline = f" ⏰{t['deadline']}" if t.get("deadline") else ""
            snoozed = " 🔕" if t.get("snoozed_until") else ""
            lines.append(f"- {t['status']} {t['title']}{deadline}{tag_str}{snoozed}")
        return "\n".join(lines)

    @registry.register(description="Mark a task as done by its ID")
    def complete_task(task_id: str) -> str:
        from .crow_state import CrowState
        db = task_db or CrowState()
        task = db.get_task(task_id)
        if not task:
            return f"Task not found: {task_id}"

        already_done = task["status"] == "done"

        if task.get("repeat") and task.get("deadline"):
            new_id = db.advance_recurring_task(task_id)
            if new_id:
                return f"✅ Marked done: {task['title']}\n🔄 Next occurrence created (id: {new_id})"
            if already_done:
                return f"✅ Already done: {task['title']}"
            return f"✅ Marked done: {task['title']}"

        if already_done:
            return f"✅ Already done: {task['title']}"

        db.update_task(task_id, status="done", snoozed_until=None)
        return f"✅ Marked done: {task['title']}"

    @registry.register(description="Snooze a task reminder. minutes: how long to pause reminders (default 60)")
    def snooze_task(task_id: str, minutes: int = 60) -> str:
        from .crow_state import CrowState
        from datetime import datetime, timedelta, timezone
        db = task_db or CrowState()
        task = db.get_task(task_id)
        if not task:
            return f"Task not found: {task_id}"
        snoozed = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
        db.update_task(task_id, snoozed_until=snoozed)
        return f"⏰ Snoozed '{task['title']}' for {minutes} min"

    @registry.register(
        description="Query the price database. Search by product name or generic name. Returns latest prices from all suppliers.",
        name="query_prices",
    )
    def query_prices(search: str = "", limit: int = 10) -> str:
        global _price_db_conn
        if _price_db_conn is None:
            _price_db_conn = os.environ.get("PRICE_DB_CONN", "")
        try:
            import psycopg2
            import psycopg2.extras
            if not _price_db_conn:
                return "Price DB not configured. Set PRICE_DB_CONN in .env"
            conn = psycopg2.connect(_price_db_conn)
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT p.name AS product, p.generic_name, s.name AS supplier,
                       pr.price, pr.currency, pr.unit, pr.scraped_at
                FROM prices pr
                JOIN sources src ON src.id = pr.source_id
                JOIN products p ON p.id = src.product_id
                JOIN suppliers s ON s.id = src.supplier_id
                WHERE (%(search)s = '' OR p.name ILIKE %(pat)s OR p.generic_name ILIKE %(pat)s)
                  AND pr.scraped_at = (
                      SELECT MAX(pr2.scraped_at) FROM prices pr2
                      WHERE pr2.source_id = pr.source_id
                  )
                ORDER BY pr.scraped_at DESC
                LIMIT %(limit)s
            """, {"search": search, "pat": f"%{search}%", "limit": limit})
            rows = cur.fetchall()
            cur.close()
            conn.close()
            if not rows:
                return f"No prices found for '{search}'"
            lines = ["| Product | Generic Name | Supplier | Price | Unit | Last Updated |"]
            lines.append("|---|---|---|---|---|---|")
            for r in rows:
                lines.append(
                    f"| {r['product']} | {r['generic_name'] or '-'} "
                    f"| {r['supplier']} | {r['currency']} {r['price']} "
                    f"| {r['unit'] or '-'} | {r['scraped_at'].strftime('%d %b %Y') if r['scraped_at'] else '-'} |"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"Price DB error: {e}"

