# Purchase bill UoM valuation correction (Odoo 19)

This standalone addon depends only on native `purchase_stock`. It neither imports
nor depends on `tha_stock_costing`; that addon is left untouched. There are no
warehouse overrides, UI changes, installation hooks, or automatic data repairs.

## What changes

On older Odoo 19 builds, when purchase receipt valuation consumes posted vendor bill lines, convert
their quantities to the stock move's unit with Odoo's `_compute_quantity` API.
Use unrounded conversions so partial billed units are not silently dropped.
Keep native monetary values and date/state filters. Convert refunds with the
opposite sign. Without a purchase line, return the upstream result unchanged.

Newer Odoo 19 builds expose native `_get_quantity_from_bill` and
`_get_value_from_bill` helpers and already normalize/allocate bill quantities.
On those builds this addon returns the upstream valuation dictionary unchanged,
preserving native split-receipt allocation, currencies and rounding. The local
Docker image has that newer implementation; the checked-out `addons/` source has
the older implementation. No core files are edited. Tests simulate the inspected
older bill method in-memory and separately exercise the newer native pass-through.
An actual live-backup replay is still required before production rollout.

BC 0001 example: 2 Box (300 Test) at 82,800 MMK per box means 600 Tests, 165,600
MMK. The old algorithm takes the full bill value and adds `(600 - 2) * 276`,
producing 330,648. With conversion, the residual quantity is zero and value is
165,600. Dynamic historical reports can change immediately on installation;
stored stock move values require a separate repair.

This is a **quantity conversion fix only**. It does not change native allocation
of a PO line's bill amounts between multiple receipts, handle currency conversion,
or redesign refunds/landed costs. On older builds a split receipt can still expose
native whole-bill allocation limitations. The historical tool blocks those cases
on all builds, even where the newer native algorithm supports them.

## Install and validate locally

Use database `THA` for the required local installation. Do not install
`tha_stock_costing` as a dependency or as part of deployment.

```powershell
docker exec odoo_19 odoo -d THA -i tha_purchase_bill_uom_valuation_fix --stop-after-init --http-port=8079 --gevent-port=8089 --max-cron-threads=0 --test-enable --test-tags /tha_purchase_bill_uom_valuation_fix
docker restart odoo_19
Get-Content .\logs\server.log -Tail 300
```

Use `-u` on subsequent upgrades. A separate fresh test database is also used to
prove this addon works without the already-installed local `tha_stock_costing`.
Fixtures roll back; they do not repair business data. See `VALIDATION.md` for
actual results, limitations and commit information.

The retired addon already present in THA can override this addon higher in the
model inheritance chain. It is not modified or removed as part of this work.
Standalone behavior is established in the separate database without it; two
boundary unit tests call this addon directly to distinguish its behavior from
that existing THA override. Production testing must use the actual live addon set.

## Historical repair: separate Odoo shell tool

`scripts/repair_valuation.py` is never imported during addon installation. Default
mode performs ORM reads only and writes a new JSON audit file. It does not modify
or unlink `stock.avco.report`, which is a report view. Applying uses ORM writes
to approved `stock.move.value` records and Odoo's current-cost refresh with
automatic manual revaluation disabled by the native method. It does not edit
purchase orders, bills, stock quantities or existing `product.value` entries.

### Before using production

1. Restore a current production backup into an isolated environment using the
   actual live Odoo build/customizations. Deploy this addon there and reproduce
   BC 0001 plus representative affected cases. Old local backups are insufficient.
2. Confirm the database name and company ID/name from that environment. Defaults
   are September 1 through September 30, 2026, **Asia/Rangoon**. `--end` is an
   exclusive local date. Never assume local IDs match live IDs.
3. Review `receipts` (eligible, unchanged, blocked) and `products` (full downstream
   timeline, month-end before/after values, current cost and reasons for blocking).
   All eligible September receipts for each selected product must be approved
   together. Other products can be handled in later batches with fresh previews.
4. Before production application take a fresh verified backup, pause stock and
   bill posting, and stop cron/other writers. The maintenance acknowledgement is
   an operator assertion: the script cannot prove writers are stopped or a backup
   is valid. Keep the window closed until fresh-session verification passes.

### Preview command

The following is a **local THA example**, not a live command. Replace
`<VERIFIED_COMPANY_ID>` and create an existing writable audit directory inside
the container or a mounted volume. In production replace database/container/path
values with verified live values. `/mnt/custom_addons` is the verified local mount.

```powershell
docker exec -e 'THA_VALUATION_REPAIR_ARGS=--database-name THA --company-id <VERIFIED_COMPANY_ID> --company-name "ID Company Ltd" --output /var/log/odoo/september-uom-preview.audit.json' -it odoo_19 odoo shell -d THA --no-http --max-cron-threads=0
```

At the Odoo shell prompt, paste this one line, then exit the shell after it returns:

```python
import runpy; _repair_result = runpy.run_path('/mnt/custom_addons/tha_purchase_bill_uom_valuation_fix/scripts/repair_valuation.py', run_name='__main__', init_globals={'env': env})
```

Each output path must be new. Download/copy the JSON and review the exact receipt
IDs, evidence, differences, downstream effects, and blocked cases. Do not edit
proposal amounts/statuses in the JSON. A fingerprint includes database UUID,
company, all relevant product moves/bills/manual histories/UoMs/quants, installed
module versions and valuation/repair source code. Any relevant change requires
a fresh preview, including changing this script after previewing.

### Explicit apply command

Only after backup-copy replay and review, start a **fresh** Odoo shell with the
following local example arguments (replace all placeholders):

```powershell
docker exec -e 'THA_VALUATION_REPAIR_ARGS=--database-name THA --company-id <VERIFIED_COMPANY_ID> --company-name "ID Company Ltd" --apply --preview-file /var/log/odoo/september-uom-preview.audit.json --move-ids <REVIEWED_RECEIPT_IDS_COMMA_SEPARATED> --maintenance-confirmed --backup-reference <VERIFIED_BACKUP_REFERENCE> --output /var/log/odoo/september-uom-applied.audit.json' -it odoo_19 odoo shell -d THA --no-http --max-cron-threads=0
```

Paste the same `runpy` line. The script locks selected products using Odoo's ORM
locking API, rechecks the original fingerprint and recomputes proposals, applies
the selected complete product groups in one transaction, checks receipt values
and native current AVCO, saves a durable before/after audit, and explicitly commits.
Errors before commit roll back the whole batch. Do not use an Odoo shell with
unrelated pending edits: the CLI owns commit/rollback of that shell transaction.

A separate `<output>.committed` marker records successful commit. An audit marked
`prepared_uncommitted` without this marker is not proof of persistence: a process
or filesystem failure can occur at the commit boundary. Check the actual database
from a fresh session before deciding whether anything must be retried.

### Eligibility and unresolved cases

Automatic repair is deliberately limited to proven old-algorithm fingerprints:
one complete purchase receipt, one posted same-company/same-currency vendor bill
line covering the complete received quantity, matching product/move units, and
no manual move override or landed cost allocation. The bill accounting date must
not be after the receipt business timestamp.

Partial bills, multiple bill lines, split receipts/backorders, refunds, returns,
foreign currency, tracked/lot-valuated/non-AVCO products, consignment, negative
stock, cross-company manual histories and ambiguous cost-reset timestamps are
blocked. An unresolved receipt for the same product, including outside September,
blocks automatic repair of that product. These cases need a separate reviewed
solution; do not remove the guards to force application.

The replay preserves earlier stored history, corrects approved receipts, then
revalues subsequent outgoing moves at chronological AVCO. It preserves manual
cost-reset dates/amounts. Any changed move with a linked accounting entry is
blocked for separate reconciliation; posted journal entries are never rewritten.
Current on-hand must reconcile with historical movement quantity. A final native
current-cost mismatch also rolls back application rather than guessing.

### Verify and resume

In a fresh shell/browser confirm each repaired receipt, September closing, later
valuation and current product cost. For the supplied BC 0001 history the expected
receipt is 165,600, post-receipt balance 629,280 / 2,280 Tests, and September
closing 463,680 / 1,680 Tests, at 276 MMK per Test. These are conditional on the
remaining history being valid. Reconcile accounting separately.

Generate a new preview: repaired receipts should be `unchanged`. Reapplying an
old preview is rejected as stale, so it cannot double-apply. Reopening a report
or obtaining a successful shell exit alone is not proof of a committed repair.
Only resume writers after verification. Restoring a backup after new business
transactions have resumed would discard those newer transactions; do not treat
restore as a harmless undo.
