---
description: Adjust line-item total prices in a Purchase Order, recalculate PO total,
  regenerate PDF, and send to Telegram. Triggered by requests to change/update/adjust
  prices in a PO.
name: po-price-adjust
triggers:
- change price
- adjust price
- update PO price
- change total price
- update price in PO
- adjust item price
- fix PO price
- correct PO amount
- change the price
- update the price
---
# PO Price Adjust — Workflow

When the user asks to change/adjust/update prices in a Purchase Order:

## Step 1: Identify the PO
- Extract the PO ID from the user's request (e.g., "PO - 062026 - 027")
- The format is `PO - MMYYYY - NNN` — with spaces and dashes
- If unclear, ask the user to confirm the PO ID

## Step 2: SSH to Laptop & View Current Items
- SSH to laptop: `ssh_exec(host="$CROWD_LAPTOP_SSH", command=...)`
- DB path from `CROWD_PROCUREMENT_DB` env var
- Query `purchase_order_items` to list all line items with their current totals:
  ```sql
  SELECT id, item_name, quantity, cost, total, uom, stock_id
  FROM purchase_order_items
  WHERE po_id = 'PO - MMYYYY - NNN'
  ORDER BY id
  ```

## Step 3: Show Current State & Confirm Changes
- Present all items in a table to the user
- Show which items need changes and the target totals
- Confirm before applying any changes

## Step 4: Apply Changes via SSH
- For each item to change, update `total` AND recalculate `cost` (cost = total / quantity):
  ```sql
  UPDATE purchase_order_items
  SET total = <new_total>, cost = ROUND(<new_total> / quantity, 4)
  WHERE id = <line_item_id> AND po_id = '<PO_ID>'
  ```
- ALWAYS update both `total` AND `cost` — cost must equal total / quantity

## Step 5: Recalculate PO Grand Total
- Sum all line item totals:
  ```sql
  SELECT SUM(total) FROM purchase_order_items WHERE po_id = '<PO_ID>'
  ```
- Update the PO header:
  ```sql
  UPDATE purchase_orders SET total = <sum> WHERE po_id = '<PO_ID>'
  ```

## Step 6: Verify
- Re-query all items and the PO total
- Cross-check: every item's `cost * quantity ≈ total` (allow rounding ±0.02)
- Compare grand total against user's target
- Flag any discrepancies to user immediately

## Step 7: Backup
- Before applying changes, copy the DB:
  ```bash
  cp "$PROCUREMENT_DB_PATH" "$PROCUREMENT_DB_PATH.bak_<PO short id>"
  ```

## Step 8: Regenerate PDF
- Use `generate_po_pdf(po_id="<PO_ID>")` — ensure this reads from the local DB copy if needed
- Verify the PDF shows correct prices by extracting text with `extract_pdf_text`

## Step 9: Send to Telegram
- Use `send_telegram(message="...", file_path="<pdf_path>")`
- Include summary of changes in the message

## Key Rules
- NEVER change `quantity` unless user explicitly asks
- ALWAYS recalculate `cost = total / quantity` when changing total
- **NEVER round up unit prices in the PDF.** Always show full precision (3+ decimal places if needed) so `cost × qty = total` exactly matches the invoice. Rounding `8.074` to `8.07` breaks the math — `8.07 × 5 = 40.35 ≠ 40.37`.
- Always backup the DB before changes
- Always verify the generated PDF shows correct numbers before sending — extract PDF text and check every line item's unit price
- If something doesn't add up, report to user immediately — don't silently fix
- **PDF must include VENDOR address (from `suppliers` table) and SHIP TO address (hospital address).** If these boxes are empty in the PDF, check `po_pdf_service.py` that `_get_po_detail()` fetches supplier data and passes `supplierAddress`, `supplierPhone`, `shipTo` to the template.
- After regenerating PDF, always verify by extracting text — confirm VENDOR box and SHIP TO box are not empty


## Usage Log
- [2026-06-18 08:02] outcome=not used
- [2026-06-18 08:39] outcome=not used
- [2026-06-18 09:30] outcome=not used
- [2026-06-18 09:33] outcome=not used
- [2026-06-18 09:36] outcome=not used
- [2026-06-18 15:21] outcome=not used
- [2026-06-19 03:45] outcome=not used
- [2026-06-19 07:53] outcome=not used
- [2026-06-19 08:24] outcome=not used
- [2026-06-19 08:34] outcome=not used
- [2026-06-19 08:53] outcome=not used
- [2026-06-19 09:09] outcome=not used
- [2026-06-19 09:44] outcome=not used
- [2026-06-19 14:56] outcome=not used
- [2026-06-19 23:55] outcome=not used
- [2026-06-20 03:18] outcome=not used
- [2026-06-20 09:28] outcome=not used
- [2026-06-20 09:39] outcome=not used
- [2026-06-20 09:41] outcome=not used
- [2026-06-20 09:44] outcome=not used
- [2026-06-20 09:48] outcome=not used
- [2026-06-20 09:52] outcome=not used
- [2026-06-20 10:24] outcome=not used
- [2026-06-20 10:31] outcome=not used
- [2026-06-20 10:36] outcome=not used
- [2026-06-20 11:09] outcome=not used
- [2026-06-21 04:24] outcome=not used
- [2026-06-21 04:45] outcome=not used
- [2026-06-21 04:55] outcome=not used
- [2026-06-21 04:55] outcome=not used
- [2026-06-21 04:55] outcome=not used
- [2026-06-21 05:08] outcome=not used
- [2026-06-21 05:09] outcome=not used
- [2026-06-21 05:09] outcome=not used
