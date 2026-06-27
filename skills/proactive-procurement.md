---
name: proactive-procurement
description: Daily proactive procurement assistant — checks items below ROP, suggests PO/RFQ drafts, waits for approval
triggers:
  - daily check
  - procurement check
  - check stock
  - reorder
  - what should i order
  - proactive procurement
  - monitor stock
---

# Proactive Procurement Assistant

## Workflow

Run daily or when user asks "what should I order?"

### Step 1: Check what needs ordering
Call `planning_advisory` to find items below ROP.

### Step 2: Check pipeline blockers
Call `pipeline_blockers` to exclude items already in open POs, RFQs, or direct orders.

### Step 3: Present findings
List items needing reorder grouped by supplier:

```
📋 Procurement Check — 17 June 2026

**3 items need reorder:**

PET ARCADE SDN BHD:
- SKU-001 Dog Food 15kg — stock 5, ROP 10, suggest 15 bags (~RM 1,800)
- SKU-003 Fish Food 1kg — stock 3, ROP 8, suggest 10 pkt (~RM 250)

PAHANG PHARMACY SDN. BHD:
- SKU-002 Cat Litter 10L — stock 2, ROP 8, suggest 15 bags (~RM 525)

**Draft PO for PET ARCADE?** (reply yes/no)
```

### Step 4: Draft PO/RFQ on approval
User says "yes" → call `draft_po` with approve=false for preview → show summary → "Approve?" → user says yes → call `draft_po` with approve=true.

### Step 5: Generate PDF and send
After approval, call `generate_po_pdf` or `generate_rfq_pdf`, then `send_telegram` with the file.

## Safety Rules

- NEVER call draft_po or draft_rfq with approve=true without explicit "yes" from user
- Always show preview first (approve=false), then ask "Approve? (yes/no)"
- If cost deviation >10% from DB cost, flag it clearly with ⚠️
- If supplier/stock_id doesn't exist in DB, report error — don't guess
- PO ID format: `PO - MMYYYY - NNN` (with spaces and dashes)
- RFQ ID format: `RFQ-MMYYYY-NN` (no spaces)

## Item JSON Format

For `draft_po` and `draft_rfq`, items_json must be a JSON array:
```json
[
  {"stock_id": "SKU-001", "quantity": 10},
  {"stock_id": "SKU-002", "quantity": 5, "cost": 34.50}
]
```
- `stock_id` and `quantity` are required
- `cost` is optional (defaults to DB cost)

## Cron Setup

Add this to Crow's cron schedule:
```json
{
  "id": "procurement-daily-check",
  "prompt": "Run proactive procurement check. Check planning_advisory, filter out pipeline_blockers, and report items needing reorder grouped by supplier. Do NOT draft anything without approval.",
  "interval_seconds": 36000,
  "enabled": true
}
```


## Usage Log
- [2026-06-19 08:18] outcome=not used
- [2026-06-19 08:34] outcome=not used
- [2026-06-19 08:53] outcome=not used
- [2026-06-20 09:35] outcome=not used
- [2026-06-20 09:39] outcome=not used
- [2026-06-21 03:28] outcome=not used
- [2026-06-21 05:09] outcome=not used
