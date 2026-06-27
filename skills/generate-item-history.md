---
description: "Generate an Item History Excel report for a PO \u2014 matches each item\
  \ to its closest historical PO (by stock_id or normalized name), shows previous\
  \ qty/cost/total with price change warnings. Output matches reference format exactly."
name: generate-item-history
triggers:
- item history
- generate item history
- history for po
- po history
- item history report
- compare prices
- price history
- show item history
- generate history
---
# Generate Item History — Workflow

When the user asks to generate/generate an item history Excel for a PO:

## Step 1: Identify the PO
- Extract PO ID from the user's request (e.g., "PO - 062026 - 027", "PO_062026_044")
- Normalize format to spaces: "PO - MMYYYY - NNN"
- If unclear, ask user to confirm

## Step 2: Call the Tool
Use `generate_item_history(po_id="PO - MMYYYY - NNN")`

This tool:
- Queries all items in the current PO
- For each item, finds the **closest previous PO** containing the same item (matched by stock_id first, then by normalized name)
- Shows **historical** qty/cost/total — NOT current PO data
- Flags price changes >10% with ⚠️ warnings
- Flags supplier changes between historical and current PO
- Generates an Excel file matching the reference format exactly

## Step 3: Send to Telegram (if needed)
- If the user wants the file, use `send_telegram(file_path="<path_from_tool_output>")`
- Include a brief summary

## Step 4: Report Results
Tell the user:
- How many items were processed
- How many had historical matches vs new items
- Any notable price changes (up or down >10%)
- Any supplier changes detected

## Key Rules
- The tool already does all the matching logic — just call it
- DO NOT try to re-implement or bypass the tool
- The output Excel has these columns: Current PO ID, Stock ID, Item Name, Matched Historical PO ID, Historical Date, Supplier, Qty, Unit Price, Total Price, Match Type, Notes / Warning
- Matching logic: stock_id first (exact), then normalized_name (fuzzy). The tool handles all of this
- Price change warnings are only for changes >10%
- If no history found, Notes says "No historical PO found"
