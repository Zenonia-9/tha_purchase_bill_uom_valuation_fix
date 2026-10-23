# Validation — 2026-10-23

Release: **19.0.1.0.5**. All results below are local; production deployment,
production-backup replay, the full September production audit, accounting
reconciliation and production data repair are pending.

## Automated checks

- Eight Python files passed AST/compile checks. Manifest depends only on native
  `purchase_stock`; no hooks, data migrations, XML views, new models or ACLs.
- Focused Odoo tests passed in **THA**: **24 tests, 0 failures, 0 errors**.
  Final log: 2026-10-23 05:38:06 UTC, process 247; post-tests 72.07 seconds.
- Focused Odoo tests passed in fresh database
  **THA_purchase_bill_uom_test_20261023**: **24 tests, 0 failures, 0 errors**.
  Final log: 2026-10-23 05:37:27 UTC, process 241; post-tests 62.46 seconds.
  `tha_stock_costing` is **uninstalled** in this database.
- THA installed/updated version independently read back as **19.0.1.0.5**.
  Existing `tha_stock_costing` remains installed at **19.0.1.7.0**, with a clean
  addon Git status; it was not modified, upgraded, uninstalled or used as a dependency.

Coverage includes the exact old BC 0001 calculation, same-unit purchases, partial
billing, refunds, ignored drafts, accounting-date cutoffs, unrounded conversions,
split-receipt blocking, preservation of upstream monetary additions, no-purchase
fallback, preview read-only behavior, explicit company/receipt selection,
complete-product approval, stale/tampered previews, new activity, linked-account
blocking, chronological outgoing corrections, manual cost resets, zero current
stock, atomic rollback and repeat-run protection.

## Runtime/source compatibility

The runtime image reports Odoo **19.0-20260630** and imports native purchase code
from `/usr/lib/python3/dist-packages/odoo/addons/purchase_stock/models/stock_move.py`.
It already has `_get_quantity_from_bill` / `_get_value_from_bill` plus native
quantity/value allocation. The checked-out `addons/purchase_stock` source has the
older raw bill-quantity algorithm. This difference was discovered during tests.

The addon therefore passes the newer native result through unchanged. Tests
simulate the inspected older bill method in memory (never editing core files)
and separately verify the newer native allocation pass-through. This simulation
does **not** prove compatibility with the complete actual live build; replay a
current production backup with its real source/customizations before rollout.

The retired addon in THA may sit above this addon in model inheritance and
re-round/reassign the result. Two boundary tests call the focused override
directly to distinguish it from that unrelated local override. The fresh
database and CLI persistence check below establish independence from that addon.

Initial development checks caught and corrected an obsolete purchase-line field
name (`taxes_id`, now `tax_ids`), missing system ZoneInfo data (use Odoo's bundled
`pytz`), and a fractional fixture quantity that Odoo rounded to zero. Final results
above are from the corrected release. Earlier failed runs remain in server.log.

## Explicit CLI commit and fresh-session verification

Synthetic transactions were created **only in the isolated test database**, using
normal purchase, receipt, bill posting and stock workflows. A historical stored
receipt value was deliberately set to 330,648 to reproduce the supplied defect.
The actual CLI then ran in separate Odoo shells without patched valuation code:

1. Default preview saved one eligible receipt, 330,648 -> 165,600, and projected
   September closing 628,728 -> 463,680 for 1,680 units at cost 276.
2. Explicit apply saved the before/after audit, committed, and created its
   completion marker. No stock quantities, bill lines or PO prices changed.
3. A fresh independent shell verified receipt **165,600**, outgoing **165,600**,
   current cost **276**, AVCO history closing **463,680 / 1,680 at 276**, and native
   dated AVCO value **463,680**.
4. A fresh preview reported **0 eligible, 1 unchanged, 0 blocked**. Reusing the
   original preview was rejected as stale, with the repaired amount unchanged.

Evidence and reproducible fixture/verification scripts are in the workspace:
`outputs/purchase_bill_uom_validation/`. The JSON audits and verification file
are explicitly synthetic, not a live September audit. The isolated database is
retained for reproduction; no existing database was deleted or replaced.

## Runtime and remaining work

The local `odoo_19` container was restarted after validation. Startup logs were
inspected: registry loaded in 30.368 seconds, and
`http://localhost:8019/web/login?db=THA` returned **HTTP 200** at 05:42:00 UTC.
No new startup error was found. This is server health evidence, not browser
verification of production stock screens.

No live addon installation, live historical repair or full workbook reconciliation
has been performed. Live September corrections require the current-backup replay,
reviewed receipt/product groups, backup and maintenance window described in README.
Blocked cases are not automatically repairable by this tool. No posted journal
entries are rewritten; accounting reconciliation remains a separate requirement.
