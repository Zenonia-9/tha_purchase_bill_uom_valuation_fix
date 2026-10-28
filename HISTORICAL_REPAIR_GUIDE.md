# Historical stock valuation repair guide

This guide covers stored valuation records affected by the billed-quantity UoM defect. Installing/upgrading the addon corrects the calculation going forward and changes dynamic reports that calculate from the corrected method. It does **not** rewrite historical `stock.move.value` records. The script below is separate from addon installation and makes no changes unless explicitly run with `--apply`.

## What the repair changes

For an eligible historical receipt, the tool changes the receipt's stored `stock.move.value` and recomputes affected later outgoing move values at chronological AVCO. It refreshes current product cost through Odoo's native method. It does not change bills, PO lines, stock quantities, or posted journal entries. Any changed move linked to an accounting entry is blocked for separate accounting reconciliation.

The tool is intentionally narrow. It only proposes the proven old-algorithm defect on a complete, single receipt with a matching posted bill line. Refunds, partial bills, multiple bill lines, split receipts/backorders, foreign currency, manual overrides, landed costs, non-AVCO/tracked products, returns, negative stock, and ambiguous histories are blocked for separate review. Do not remove guards to force a repair.

## 1. Reproduce against a current backup first

Restore a current production backup into an isolated database with the actual live Odoo build and installed custom addons. Confirm the database identity, target company ID/name, addon versions, and that the live build has the quantity-conversion defect. Install/upgrade the focused addon in the isolated database and compare BC 0001 Albumin (receipt `MWH/IN/00083`, bill `BILL/26-27/09/0083`) plus other representative receipts.

Do not assume database record IDs match between backup copies and production. The local database and command examples below are demonstrations only.

## 2. Create a read-only preview

Choose an existing audit directory writable from inside the Odoo container (or a mounted volume). Use a new output filename for every run. Set verified database and company values. September dates below use Asia/Rangoon; `--end` is exclusive.

Example for local THA only:

```powershell
docker exec -e 'THA_VALUATION_REPAIR_ARGS=--database-name THA --company-id <VERIFIED_COMPANY_ID> --company-name "ID Company Ltd" --output /var/log/odoo/september-uom-preview.audit.json' -it odoo_19 odoo shell -d THA --no-http --max-cron-threads=0
```

At the shell prompt, run:

```python
import runpy; _repair_result = runpy.run_path('/mnt/custom_addons/tha_purchase_bill_uom_valuation_fix/scripts/repair_valuation.py', run_name='__main__', init_globals={'env': env})
```

Then exit the shell. Preview performs ORM reads and writes only a new JSON audit file. It does not modify business records. Copy the JSON out of the container and review every `receipts` row and `products` timeline: eligible/unchanged/blocked status, exact receipt IDs and before/after amounts, month-end values, current cost, and all blocking reasons. Approve complete eligible receipt groups per product; a product with another unresolved September receipt is blocked as a group. Do not edit proposal data in the JSON.

## 3. Resolve blocked cases separately

Do not include blocked receipt IDs in an apply request. Investigate each reason using the original documents and full stock/accounting history. Where a journal entry is linked, reconcile that accounting impact separately; the tool never edits posted journal entries. If the preview reveals new stock/bill activity, changed addon code or configuration, or any mismatch, generate a new preview and review it from scratch.

## 4. Prepare the production maintenance window

Only after the isolated backup-copy replay and full preview review:

1. Take and verify a fresh production backup; record its reference.
2. Schedule a maintenance window and pause stock/bill posting and other writers. Stop cron/other processes that can change the reviewed records.
3. Confirm the production database, company ID/name, container, mounted addon path, audit directory, and exact approved move IDs. The script cannot verify that the backup is restorable or that other writers are stopped; the operator must confirm these.

Keep writers paused through apply and fresh-session checks.

## 5. Apply only reviewed receipt IDs

Start a **new** Odoo shell and point `--preview-file` to the reviewed preview inside the container. Specify all eligible September receipt IDs for each selected product, the explicit maintenance acknowledgement, a verified backup reference, and a new audit path. Example shape (replace every placeholder; local THA values are not production values):

```powershell
docker exec -e 'THA_VALUATION_REPAIR_ARGS=--database-name THA --company-id <VERIFIED_COMPANY_ID> --company-name "ID Company Ltd" --apply --preview-file /var/log/odoo/september-uom-preview.audit.json --move-ids <REVIEWED_RECEIPT_IDS_COMMA_SEPARATED> --maintenance-confirmed --backup-reference <VERIFIED_BACKUP_REFERENCE> --output /var/log/odoo/september-uom-applied.audit.json' -it odoo_19 odoo shell -d THA --no-http --max-cron-threads=0
```

At the shell prompt, run the same `runpy` line from step 2, then exit. The script locks the selected products, checks the preview fingerprint and recomputes proposals before writing. Any changed database/code state, incomplete product group, blocked case, or post-check failure aborts the transaction. Do not run it in a shell with unrelated pending work.

Keep both the applied JSON audit and its `.committed` marker. The marker is written after commit. An audit in `prepared_uncommitted` state without a marker is not proof of persistence; inspect the database from a fresh session before deciding whether to retry.

## 6. Verify from a fresh session before resuming work

Open a new shell/browser session and verify:

- Each approved receipt has the proposed stored value.
- The product's September closing quantity/value and later valuation timeline match the approved audit.
- Current quantity and AVCO cost match the approved result.
- Bills, PO quantities/prices, and stock quantities remain unchanged.
- A new preview marks repaired receipts `unchanged`; the old preview must be rejected as stale.
- Accounting entries and stock reports have been reconciled separately where required.

For the reported BC 0001 history, conditional on all other history being valid, the expected values are receipt **165,600 MMK**, post-receipt **629,280 MMK / 2,280 Tests**, and September closing **463,680 MMK / 1,680 Tests**, at **276 MMK/Test**. Compare against the actual approved preview and source documents; do not assume those figures apply to other products.

Resume stock/bill posting and cron only after checks pass. If restoration is needed after new transactions have resumed, coordinate reconciliation first; restoring a backup can discard transactions created since that backup.

## 7. Keep evidence

Retain the source backup reference, reviewed preview, applied audit, `.committed` marker, fresh-session verification, and accounting reconciliation with the maintenance record. A successful command exit or reopened report alone does not prove a committed repair.
