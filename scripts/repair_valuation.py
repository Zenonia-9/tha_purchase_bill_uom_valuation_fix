"""Explicit Odoo-shell audit/repair. Never imported by the installed addon.

Preview performs ORM reads only. apply_preview writes inside a savepoint but does
not commit; only the CLI commits, after saving the complete audit to a new file.
"""
import argparse
import hashlib
import inspect
import json
import os
import shlex
from datetime import datetime
from pathlib import Path

import pytz

from odoo import fields


MODULE = "tha_purchase_bill_uom_valuation_fix"
SCHEMA = 1


def _json(value):
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _utc_day(day):
    # Odoo ships pytz; minimal Docker images need not have system zoneinfo/tzdata.
    return pytz.timezone("Asia/Rangoon").localize(
        datetime.strptime(day, "%Y-%m-%d")
    ).astimezone(pytz.UTC).replace(tzinfo=None)


def _company_env(env, company_id, company_name):
    company = env["res.company"].browse(company_id).exists()
    if len(company) != 1 or company.name != company_name:
        raise ValueError("Company ID/name does not match the reviewed target")
    # Shell callers must not accidentally repair a warehouse-filtered subset.
    context = dict(env.context, allowed_company_ids=[company.id])
    for key in ("warehouse_id", "location", "lot_id", "owner_id", "package_id", "to_date", "at_date"):
        context.pop(key, None)
    env = env(context=context)
    installed = env["ir.module.module"].search([
        ("name", "=", MODULE), ("state", "=", "installed"),
    ])
    if not installed:
        raise ValueError("Install the focused addon before previewing or applying")
    return env, company


def _snapshot(env, company, products, candidates):
    """Include downstream/new activity, invoice state, UoM factors and code drift."""
    moves = env["stock.move"].search([
        ("company_id", "=", company.id), ("product_id", "in", products.ids),
    ], order="id")
    purchase_lines = moves.purchase_line_id
    invoice_lines = purchase_lines.invoice_lines
    values = env["product.value"].search([
        ("product_id", "in", products.ids),
    ], order="id")
    quants = env["stock.quant"].search([
        ("company_id", "=", company.id), ("product_id", "in", products.ids),
    ], order="id")
    uoms = moves.product_uom | invoice_lines.product_uom_id | products.uom_id
    modules = env["ir.module.module"].search([("state", "=", "installed")], order="name")
    methods = ("_get_value_from_account_move", "_get_value_data", "_run_avco",
               "_get_value_from_quotation", "_get_manual_value", "_update_standard_price",
               "_get_stock_move_price_unit", "_get_quantity_from_bill", "_get_value_from_bill")
    code = []
    for model in (env["stock.move"], env["product.product"], env["purchase.order.line"]):
        for cls in type(model).__mro__:
            for name in methods:
                method = cls.__dict__.get(name)
                if method:
                    code.append([cls.__module__, name, inspect.getsource(method)])
    landed = []
    if "stock.valuation.adjustment.lines" in env:
        adjustments = env["stock.valuation.adjustment.lines"].search([
            ("move_id", "in", moves.ids),
        ], order="id")
        landed = adjustments.read(["move_id", "additional_landed_cost", "write_date", "cost_id"])
        landed += adjustments.cost_id.read(["state", "date", "write_date"])
    return {
        "database": env.cr.dbname,
        "database_uuid": env["ir.config_parameter"].sudo().get_param("database.uuid"),
        "company": company.read(["name", "currency_id", "write_date"]),
        "candidate_ids": candidates.ids,
        "products": products.read(["default_code", "uom_id", "tracking", "lot_valuated",
                                     "standard_price", "cost_method", "write_date"]),
        "moves": moves.read(["product_id", "state", "date", "quantity", "product_uom",
                             "value", "is_in", "is_out", "is_dropship", "origin_returned_move_id",
                             "purchase_line_id", "account_move_id", "write_date"]),
        "move_lines": moves.move_line_ids.sorted("id").read([
            "move_id", "quantity", "product_uom_id", "owner_id", "lot_id", "write_date",
        ]),
        "purchase_lines": purchase_lines.sorted("id").read([
            "product_id", "product_qty", "product_uom_id", "price_unit", "discount",
            "currency_id", "tax_ids", "write_date",
        ]),
        "bills": invoice_lines.move_id.sorted("id").read([
            "state", "move_type", "date", "currency_id", "company_id", "write_date",
        ]),
        "bill_lines": invoice_lines.sorted("id").read([
            "product_id", "quantity", "product_uom_id", "price_subtotal", "date", "write_date",
        ]),
        "uoms": uoms.sorted("id").read(["factor", "rounding", "write_date"]),
        "manual_values": values.read([
            "product_id", "company_id", "lot_id", "move_id", "value", "date", "write_date",
        ]),
        "quants": quants.read(["quantity", "location_id", "owner_id", "write_date"]),
        "modules": modules.read(["name", "latest_version"]),
        "code": _digest(code),
        "repair_code": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "landed": landed,
    }


def _receipt_row(env, move, company):
    po_line = move.purchase_line_id
    bills = po_line.invoice_lines.filtered(lambda line: line.move_id.state == "posted")
    reasons = []
    if len(po_line.move_ids.filtered(lambda item: item.state != "cancel")) != 1:
        reasons.append("split receipt / return / open backorder")
    if len(bills) != 1 or any(line.move_id.move_type != "in_invoice" for line in bills):
        reasons.append("requires exactly one posted vendor bill line; refunds need review")
    if po_line.currency_id != company.currency_id or any(
        line.currency_id != company.currency_id for line in bills
    ):
        reasons.append("foreign currency")
    if move.product_uom != move.product_id.uom_id:
        reasons.append("move and product valuation units differ")
    if env["product.value"].search_count([("move_id", "=", move.id)]):
        reasons.append("manual receipt valuation override")
    if "stock.valuation.adjustment.lines" in env and env["stock.valuation.adjustment.lines"].search_count([
        ("move_id", "=", move.id),
    ]):
        reasons.append("landed cost allocation")
    raw = sum(bills.mapped("quantity"))
    converted = sum((line.product_uom_id or line.product_id.uom_id)._compute_quantity(
        line.quantity, move.product_uom, round=False,
    ) for line in bills)
    quantity = move._get_valued_qty()
    if any(line.product_id != move.product_id or line.company_id != company for line in bills):
        reasons.append("bill product/company differs")
    if not quantity or abs(quantity - converted) > 1e-7:
        reasons.append("partial billing or billed/received quantity mismatch")
    if abs(quantity - move.quantity) > 1e-7:
        reasons.append("valued quantity differs from move quantity")
    if any(fields.Datetime.to_datetime(line.date) > move.date for line in bills):
        reasons.append("bill accounting date is after receipt; historical timing needs review")
    amount = sum(bills.mapped("price_subtotal"))
    price = po_line._get_stock_move_price_unit()
    legacy = amount + (quantity - raw) * price
    corrected = move._get_value()
    if quantity < raw or company.currency_id.compare_amounts(amount, corrected):
        reasons.append("corrected calculation differs from full bill amount")
    differs = bool(company.currency_id.compare_amounts(move.value, corrected))
    if differs and company.currency_id.compare_amounts(move.value, legacy):
        reasons.append("stored value does not match the unconverted-quantity defect")
    if differs and abs(raw - converted) < 1e-7:
        reasons.append("difference is not a unit conversion defect")
    return {
        "move_id": move.id, "reference": move.reference, "date": str(move.date),
        "product_id": move.product_id.id, "product": move.product_id.display_name,
        "purchase": po_line.order_id.name, "bill_ids": bills.move_id.ids,
        "bills": bills.move_id.mapped("name"), "bill_uoms": bills.product_uom_id.mapped("name"),
        "stock_uom": move.product_uom.name, "quantity": quantity,
        "raw_bill_quantity": raw, "converted_bill_quantity": converted,
        "bill_value": amount, "legacy_value": legacy, "stored_value": move.value,
        "proposed_value": corrected, "difference": corrected - move.value,
        "status": "blocked" if reasons else ("eligible" if differs else "unchanged"),
        "reasons": reasons,
    }


def _replay(env, product, company, receipt_values, closing_at):
    """Replay stored history; manual cost resets are explicit event boundaries.

    This intentionally rejects negative stock and ambiguous events rather than
    claiming to solve Odoo's unrelated historical valuation edge cases.
    """
    reasons, changes, timeline = [], [], []
    if product.cost_method != "average" or product.lot_valuated or product.tracking != "none":
        reasons.append("automatic repair supports untracked, non-lot-valuated AVCO only")
    moves = env["stock.move"].search([
        ("company_id", "=", company.id), ("product_id", "=", product.id),
        ("state", "=", "done"), "|", "|", ("is_in", "=", True),
        ("is_out", "=", True), ("is_dropship", "=", True),
    ], order="date, id")
    manual = env["product.value"].search([
        ("product_id", "=", product.id), ("move_id", "=", False), ("lot_id", "=", False),
    ], order="date, id")
    if any(value.company_id != company for value in manual):
        reasons.append("cross-company manual cost history")
    if env["product.value"].search_count([("move_id", "in", moves.ids)]):
        reasons.append("manual move valuation override in product history")
    if any(move.is_dropship or move.origin_returned_move_id for move in moves):
        reasons.append("dropship or return history")
    if any(move.product_uom != product.uom_id or move.move_line_ids.owner_id or any(
        line.product_uom_id != product.uom_id for line in move.move_line_ids
    ) for move in moves):
        reasons.append("mixed stock units or consignment history")
    for move in moves.filtered(lambda item: item.is_in and item.purchase_line_id):
        if move.id not in receipt_values and company.currency_id.compare_amounts(move.value, move._get_value()):
            reasons.append("unresolved incoming valuation outside the approved September repairs")
    events = [(move.date, 1, move.id, move) for move in moves]
    events += [(value.date, 0, value.id, value) for value in manual]
    if set(moves.mapped("date")) & set(manual.mapped("date")):
        reasons.append("manual cost and stock move share an ambiguous timestamp")
    old_qty = new_qty = old_value = new_value = 0.0
    new_cost = 0.0
    closing = None
    first_changed = min((move.date for move in moves if move.id in receipt_values), default=None)
    for date, kind, record_id, record in sorted(events, key=lambda event: event[:3]):
        if closing is None and date >= closing_at:
            closing = {"quantity": new_qty, "before": old_value, "after": new_value}
        if kind == 0:
            old_value, new_value = old_qty * record.value, new_qty * record.value
            new_cost = record.value
        else:
            quantity = record.quantity
            before = after = record.value
            if record.is_in:
                after = receipt_values.get(record_id, before)
                old_qty += quantity
                new_qty += quantity
                old_value += before
                new_value += after
                new_cost = new_value / new_qty if new_qty else 0.0
            else:
                # Preserve existing outgoing values before the first corrected receipt.
                if first_changed and date >= first_changed:
                    after = company.currency_id.round(quantity * new_cost)
                old_qty -= quantity
                new_qty -= quantity
                old_value -= before
                new_value -= after
            if new_qty < -1e-7 or quantity < 0:
                reasons.append("negative stock history")
            if company.currency_id.compare_amounts(before, after):
                changes.append({"move_id": record_id, "reference": record.reference,
                                "date": str(date), "incoming": record.is_in,
                                "before": before, "after": after,
                                "account_move_id": record.account_move_id.id})
                if record.account_move_id:
                    reasons.append("changed move has linked accounting entry; reconcile separately")
        if first_changed and date >= first_changed:
            timeline.append({"date": str(date), "model": record._name, "id": record_id,
                             "quantity": new_qty, "before": old_value, "after": new_value,
                             "avco_after": new_value / new_qty if new_qty else 0.0})
    if closing is None:
        closing = {"quantity": new_qty, "before": old_value, "after": new_value}
    if abs(product.qty_available - new_qty) > 1e-7:
        reasons.append("history quantity does not reconcile with current on-hand")
    if abs(new_qty) < 1e-7 and abs(new_value) > company.currency_id.rounding:
        reasons.append("nonzero valuation at zero stock")
    return {"product_id": product.id, "product": product.display_name,
            "reasons": sorted(set(reasons)), "changes": changes, "timeline": timeline,
            "closing": closing, "current_quantity": new_qty,
            "current_value_before": old_value, "current_value_after": new_value,
            "current_cost_before": product.standard_price,
            # Native AVCO retains its last unit cost when an outgoing move
            # consumes all stock. It does not reset the product cost to zero.
            "current_cost_after": new_cost}


def build_preview(env, company_id, company_name="ID Company Ltd",
                  start="2026-09-01", end="2026-10-01"):
    env, company = _company_env(env, company_id, company_name)
    start_at, end_at = _utc_day(start), _utc_day(end)
    if start_at >= end_at:
        raise ValueError("End must be after start (exclusive local date)")
    candidates = env["stock.move"].search([
        ("company_id", "=", company.id), ("state", "=", "done"), ("is_in", "=", True),
        ("purchase_line_id", "!=", False), ("date", ">=", start_at), ("date", "<", end_at),
    ], order="id")
    rows = [_receipt_row(env, move, company) for move in candidates]
    effects = []
    for product in candidates.product_id.sorted("id"):
        product_rows = [row for row in rows if row["product_id"] == product.id]
        eligible = [row for row in product_rows if row["status"] == "eligible"]
        if not eligible:
            continue
        effect = _replay(env, product, company,
                         {row["move_id"]: row["proposed_value"] for row in eligible}, end_at)
        if any(row["status"] == "blocked" for row in product_rows):
            effect["reasons"].append("another September receipt for this product is unresolved")
        if effect["reasons"]:
            for row in eligible:
                row["status"] = "blocked"
                row["reasons"] += effect["reasons"]
        effects.append(effect)
    snapshot = _snapshot(env, company, candidates.product_id.sorted("id"), candidates)
    return {"schema": SCHEMA, "database": env.cr.dbname, "company_id": company.id,
            "company_name": company.name, "start": start, "end_exclusive": end,
            "timezone": "Asia/Rangoon", "generated_at": str(fields.Datetime.now()),
            "receipts": rows, "products": effects, "snapshot": snapshot,
            "fingerprint": _digest(snapshot)}


def apply_preview(env, preview, move_ids):
    """Apply complete product groups atomically. Caller owns commit/audit storage."""
    if preview.get("schema") != SCHEMA or preview.get("database") != env.cr.dbname:
        raise ValueError("Preview schema/database mismatch")
    if _digest(preview["snapshot"]) != preview["fingerprint"]:
        raise ValueError("Preview fingerprint is invalid")
    env, company = _company_env(env, preview["company_id"], preview["company_name"])
    selected = set(move_ids)
    eligible = {row["move_id"]: row for row in preview["receipts"] if row["status"] == "eligible"}
    if not selected or not selected <= eligible.keys():
        raise ValueError("Select explicit eligible receipt IDs from the preview")
    product_ids = {eligible[move_id]["product_id"] for move_id in selected}
    required = {move_id for move_id, row in eligible.items() if row["product_id"] in product_ids}
    if selected != required:
        raise ValueError("Select all eligible September receipts for each selected product")
    with env.cr.savepoint():
        products = env["product.product"].browse(sorted(product_ids))
        locked = products.try_lock_for_update()
        if set(locked.ids) != product_ids:
            raise ValueError("Product is busy; stop stock processing and retry")
        env.invalidate_all()
        fresh = build_preview(env, company.id, company.name, preview["start"], preview["end_exclusive"])
        if fresh["fingerprint"] != preview["fingerprint"]:
            raise ValueError("Stale preview: database, activity, configuration or code changed")
        # Never trust editable proposal amounts/statuses in the supplied JSON.
        if fresh["receipts"] != preview["receipts"] or fresh["products"] != preview["products"]:
            raise ValueError("Preview proposals differ from fresh verified calculations")
        effects = [item for item in fresh["products"] if item["product_id"] in product_ids]
        for effect in effects:
            if effect["reasons"]:
                raise ValueError("Blocked product cannot be repaired")
            for change in effect["changes"]:
                move = env["stock.move"].browse(change["move_id"])
                move.write({"value": change["after"]})
        products._update_standard_price()
        for effect in effects:
            product = env["product.product"].browse(effect["product_id"])
            if abs(product.standard_price - effect["current_cost_after"]) > 1e-6:
                raise ValueError("Native current AVCO differs from replay; all changes rolled back")
        after = build_preview(env, company.id, company.name, preview["start"], preview["end_exclusive"])
        if any(row["move_id"] in selected and row["status"] != "unchanged" for row in after["receipts"]):
            raise ValueError("Receipt post-check failed; all changes rolled back")
        return {"schema": SCHEMA, "state": "prepared_uncommitted", "before": preview,
                "selected_move_ids": sorted(selected), "applied_products": effects, "after": after}


def _save_new(path, data):
    with Path(path).open("x", encoding="utf-8") as output:
        output.write(_json(data) + "\n")
        output.flush()
        os.fsync(output.fileno())


def main(env, argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-name", required=True)
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--company-name", default="ID Company Ltd")
    parser.add_argument("--start", default="2026-09-01")
    parser.add_argument("--end", default="2026-10-01")
    parser.add_argument("--output", required=True, help="New audit JSON file; never overwritten")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--preview-file")
    parser.add_argument("--move-ids", help="Comma-separated reviewed receipt IDs")
    parser.add_argument("--maintenance-confirmed", action="store_true")
    parser.add_argument("--backup-reference")
    args = parser.parse_args(argv if argv is not None else shlex.split(
        os.environ.get("THA_VALUATION_REPAIR_ARGS", "")))
    if args.database_name != env.cr.dbname:
        raise ValueError("Connected database does not match --database-name")
    if not args.apply:
        preview = build_preview(env, args.company_id, args.company_name, args.start, args.end)
        _save_new(args.output, preview)
        print("Preview saved; no database writes. Receipt counts:", {
            status: sum(row["status"] == status for row in preview["receipts"])
            for status in ("eligible", "unchanged", "blocked")})
        return
    if not all((args.preview_file, args.move_ids, args.maintenance_confirmed, args.backup_reference)):
        raise ValueError("Apply requires preview, explicit move IDs, maintenance confirmation and backup reference")
    if Path(args.output).exists() or Path(args.output + ".committed").exists():
        raise ValueError("Apply audit/commit marker paths must both be new")
    preview = json.loads(Path(args.preview_file).read_text(encoding="utf-8"))
    if preview["company_id"] != args.company_id or preview["company_name"] != args.company_name:
        raise ValueError("CLI company does not match preview")
    try:
        audit = apply_preview(env, preview, [int(item) for item in args.move_ids.split(",")])
        audit["backup_reference"] = args.backup_reference
        _save_new(args.output, audit)
        env.cr.commit()
    except BaseException:
        env.cr.rollback()
        raise
    # The JSON is durable before commit. Append completion without destroying it.
    _save_new(args.output + ".committed", {"state": "committed", "database": env.cr.dbname,
                                         "after_fingerprint": audit["after"]["fingerprint"]})
    print("Committed. Keep audit and completion marker; verify in a fresh session:", args.output)


if __name__ == "__main__":
    main(env)
