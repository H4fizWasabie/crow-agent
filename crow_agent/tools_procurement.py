"""Procurement tools — read-only SQL queries on the procurement database.

DB must be synced to the path set in PROCUREMENT_DB_PATH env var.
Crow NEVER writes to this DB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import os

# Env override for local dev. Default: VPS path.
DB_PATH = Path(
    os.environ.get("PROCUREMENT_DB_PATH", "/opt/crow-agent/data/procurement/procurement.db")
)

# Tables and their columns — injected into tool descriptions so LLM writes correct SQL
SCHEMA_REFERENCE = """
Tables:
- items(stock_id, item_name, cost, uom, product_type, category, current_stock, rop, selling_price, last_updated, pack_size, exclude, product_status, velocity_override, supplier_name, item_behaviour)
- suppliers(supplier_name, contact_person, phone, email, address, payment_terms, brn, account_no, bank_name)
- purchase_orders(po_id, date, supplier, bill_no, total, paid, balance, status, ship_status, quotation_url, invoice_url, department, terms, signed_url, linked_rfq, payment_url, item_history_url, po_pdf_url, raw_po_json)
- purchase_order_items(id, po_id, item_name, quantity, cost, total, uom, stock_id)
- order_requests(id, request_date, requested_by, item_name, stock_id, quantity, department_or_area, supplier_name, priority, notes, status, linked_po_id, created_at, updated_at)
- rfq_logs(rfq_id, date, supplier, items_count, created_by, signed_url, raw_rfq_json)
- stock_movements(id, stock_id, item_name, year, month, in_qty, out_qty, adj_in, adj_out, report_closing)
- invoices(id, invoice_no, invoice_date, supplier, po_no, po_date, do_no, do_date, department, date_received, total_amount, doc_url, timestamp, raw_invoice_json, source_sheet)
- catalogue_items(id, source_id, item_name, stock_id, unit_price, uom, pack_size, supplier_name, deal_count, freshness_status)
- catalogue_deals(id, catalogue_item_id, min_qty, max_qty, deal_unit_price, free_text_rule, source_text, parse_confidence, is_active)

PO statuses: Processing, Pending Approval, Approved, Pending Payment, Partial, Paid, VOID
Ship statuses: Pending, Shipped, Received
Order request statuses: Pending, Approved, Ordered, Declined, Cancelled
"""


def _connect() -> sqlite3.Connection:
    """Open read-only connection."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _format_rows(rows: list[sqlite3.Row], limit: int = 40) -> str:
    """Format query results as text table."""
    if not rows:
        return "No results."
    keys = rows[0].keys()
    header = " | ".join(keys)
    sep = "-" * len(header)
    lines = [header, sep]
    for row in rows[:limit]:
        values = [str(row[k]) for k in keys]
        lines.append(" | ".join(values))
    if len(rows) > limit:
        lines.append(f"... ({len(rows) - limit} more rows)")
    return "\n".join(lines)


def _is_readonly(sql: str) -> bool:
    """Block any write operations."""
    dangerous = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE", "TRUNCATE"}
    upper = sql.upper().strip()
    for word in dangerous:
        if upper.startswith(word) or f" {word} " in f" {upper} ":
            return False
    return True


def register_tools(registry: Any, **kwargs: Any) -> None:
    """Register procurement tools on the given registry."""

    @registry.register(
        description=f"Run a read-only SQL query on the procurement database. Returns formatted results. Use SELECT only.\n\n{SCHEMA_REFERENCE}"
    )
    def query_procurement(sql: str) -> str:
        """Execute read-only SQL on the synced procurement DB."""
        if not _is_readonly(sql):
            return "Error: Only SELECT queries allowed. This tool is read-only."
        if not DB_PATH.exists():
            return "Error: Procurement database not synced yet. Try again later."
        try:
            conn = _connect()
            rows = conn.execute(sql).fetchall()
            conn.close()
            return _format_rows(rows)
        except Exception as exc:
            return f"Error: {exc}"

    @registry.register(
        description="Get procurement dashboard summary: PO counts by status, total value, unpaid balance, low stock items, pending approvals."
    )
    def procurement_dashboard() -> str:
        """Return a dashboard summary matching Procura's KPI cards."""
        if not DB_PATH.exists():
            return "Error: Procurement database not synced yet."
        try:
            conn = _connect()
            total_pos = conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0]
            total_value = conn.execute("SELECT COALESCE(SUM(total), 0) FROM purchase_orders").fetchone()[0]
            unpaid = conn.execute(
                "SELECT COALESCE(SUM(balance), 0) FROM purchase_orders WHERE LOWER(COALESCE(status,'')) IN ('pending payment','partial','approved','processing')"
            ).fetchone()[0]
            low_stock = conn.execute(
                "SELECT COUNT(*) FROM items WHERE current_stock <= rop AND rop > 0 AND COALESCE(exclude, 0) = 0"
            ).fetchone()[0]

            statuses = conn.execute(
                """SELECT 
                    LOWER(COALESCE(status, '')) as st, 
                    COUNT(*) as cnt 
                FROM purchase_orders 
                GROUP BY LOWER(COALESCE(status, ''))"""
            ).fetchall()
            status_map = {r["st"]: r["cnt"] for r in statuses}

            conn.close()

            lines = [
                f"Total POs: {total_pos}",
                f"Total Value: RM {total_value:,.2f}",
                f"Unpaid Balance: RM {unpaid:,.2f}",
                f"Items Below ROP: {low_stock}",
                "",
                "PO Status Breakdown:",
            ]
            for status in ["processing", "pending approval", "approved", "pending payment", "partial", "paid", "void"]:
                count = status_map.get(status, 0)
                lines.append(f"  {status.title()}: {count}")

            return "\n".join(lines)
        except Exception as exc:
            return f"Error: {exc}"
