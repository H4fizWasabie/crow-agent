---
name: procurement-process
description: The complete procurement lifecycle state machine — from planning to payment. Crow must understand this to track items, escalate issues, and avoid wrong suggestions.
triggers:
  - procurement process
  - procurement lifecycle
  - state machine
  - where is item
  - PO status
  - procurement workflow
---

# Procurement Process — State Machine

## Overview

```
EXCEL IMPORT → PLANNING → RFQ → PO PROCESSING → PENDING PAYMENT → PAID (complete)
                  ↑          ↑                      ↑                  ↑
            Crow suggests  Crow generates      Crow monitors     Crow monitors
            items to order RFQ PDF             for overdue       for aging
```

## Stage 1: Daily Excel Import

Every morning, Excel stock reports are downloaded from hospital systems and imported into Procura. This updates `items.current_stock` and `items.last_updated`. **Crow does not participate in this stage.**

## Stage 2: Planning — What Needs Ordering?

### How it works
1. Call `planning_advisory` — returns items where `current_stock <= rop`
2. Items already in pipeline (open PO or recent RFQ) are automatically excluded
3. Items grouped by supplier with suggested quantities and estimated budget

### Crow's role
- Run `planning_advisory` daily (via cron or manual trigger)
- Present findings: "3 items need reorder from PET ARCADE. Draft RFQ?"
- When user says yes → draft RFQ (Stage 3)

### Key rules
- Never suggest items already in pipeline_blockers
- Group by supplier — one RFQ per supplier
- Use DB cost unless user specifies alternative

## Stage 3: RFQ — Request for Quotation

### How it works
1. `draft_rfq` creates an rfq_logs entry with auto-assigned ID (`RFQ-MMYYYY-NN`)
2. Generate PDF via `generate_rfq_pdf`
3. Send PDF to supplier via Telegram or email
4. **Once RFQ is created, items are EXCLUDED from planning until RFQ is >7 days old**

### Crow's role
- Generate RFQ PDF on request
- Send to Telegram
- Supply reference: RFQ ID, items, quantities

### Key rules
- RFQ ID format: `RFQ-MMYYYY-NN` (no spaces)
- raw_rfq_json format: `[{"id":"STOCK_ID","n":"ITEM_NAME","u":"UOM","q":QTY}]`
- Validate supplier exists in `suppliers` table before creating
- Validate stock_id exists in `items` table
- After creating, items are pipeline-blocked (won't appear in planning)

### What Crow does NOT do
- Send RFQ to supplier (user does this)
- Follow up with supplier

## Stage 4: PO — Processing (MANUAL — Crow tracks only)

### How it works
1. Supplier responds with quotation/invoice
2. User manually creates PO in Procura
3. User manually changes PO status to Processing → Pending Payment
4. **This stage is entirely manual. Crow does NOT create POs or change statuses.**

### Crow's role
- `procurement_alerts`: flag Processing POs > 7 days old as overdue
- `pipeline_blockers`: items in Processing POs are excluded from planning
- If user asks "draft PO for X", Crow can use `draft_po` tool

### Key rules
- PO ID format: `PO - MMYYYY - NNN` (with spaces and dashes)
- Cost deviation >10% from DB cost → flag in draft
- Processing PO older than 7 days → alert

## Stage 5: Pending Payment — Escalate if Aging

### How it works
1. PO PDF + invoice + item history sent to finance
2. User manually changes status to Pending Payment
3. Finance processes payment
4. Finance sends payment receipt via WhatsApp
5. User uploads receipt to Procura → status becomes Paid

### Crow's role
- `procurement_alerts`: flag Pending Payment > 7 days as aging
- Escalate: "PO-062026-011 still pending payment after 13 days — follow up with finance?"

### Key rules
- Pending Payment > 7 days → escalate
- Balance column shows remaining unpaid amount

## Stage 6: Paid — Complete

### How it works
1. User uploads payment receipt to Procura
2. PO status changes to Paid
3. `ship_status` tracks physical delivery separately: Pending → Shipped → Received

### Crow's role
- `procurement_alerts`: flag Shipped > 7 days not yet Received
- "PO-062026-005 shipped 9 days ago but not marked as Received"

## Escalation Rules (procurement_alerts)

| Condition | Threshold | Alert |
|-----------|-----------|-------|
| Processing PO | > 7 days | Overdue — needs action |
| Pending Payment | > 7 days | Aging — follow up with finance |
| Shipped, not Received | > 7 days | Stalled shipment |

## DB Schema Reference

Key tables Crow uses:
- `items` — master stock list with stock_id, current_stock, rop, cost
- `suppliers` — supplier details with bank info
- `purchase_orders` — POs with po_id, supplier, status, ship_status, total, paid, balance
- `purchase_order_items` — line items per PO
- `rfq_logs` — RFQ history with rfq_id, supplier, raw_rfq_json
- `stock_movements` — monthly consumption data for ROP calculation

PO statuses (current, simplified): Processing, Pending Payment, Paid, VOID
Ship statuses: Pending, Shipped, Received
Legacy PO statuses (no longer used): Pending Approval, Approved, Partial

## What Crow Must NEVER Do

1. NEVER change PO statuses — this is manual in Procura
2. NEVER create PO entries without explicit "yes" from user
3. NEVER auto-send RFQ to supplier — user handles supplier communication
4. NEVER suggest items that are already in pipeline_blockers
5. NEVER assume supplier contact details — always verify against `suppliers` table


## Usage Log
- [2026-06-19 08:34] outcome=not used
- [2026-06-19 08:53] outcome=not used
- [2026-06-20 09:28] outcome=not used
- [2026-06-20 09:39] outcome=not used
- [2026-06-20 09:44] outcome=not used
- [2026-06-20 11:09] outcome=not used
- [2026-06-21 04:55] outcome=not used
- [2026-06-21 05:09] outcome=not used
