import copy
import json
from unittest.mock import patch

from freezegun import freeze_time

from odoo import Command, fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.purchase_stock.models.stock_move import StockMove as NativePurchaseMove
from odoo.tests import tagged

from ..scripts.repair_valuation import apply_preview, build_preview
from ..models.stock_move import StockMove as FocusedStockMove


def _legacy_account_value(move, quantity, at_date=None):
    """Older checked-out purchase_stock algorithm; runtime image is newer.

    This reproduces its full-value/raw-quantity inputs without depending on the
    retired tha_stock_costing addon or changing any core file on disk.
    """
    data = {"quantity": 0.0, "value": 0.0, "description": False}
    for line in move.purchase_line_id.invoice_lines:
        if line.move_id.state != "posted" or (
            at_date and fields.Datetime.to_datetime(line.date) > at_date
        ):
            continue
        sign = {"in_invoice": 1, "in_refund": -1}.get(line.move_id.move_type, 0)
        data["quantity"] += sign * line.quantity
        data["value"] += sign * line.price_subtotal
    return data


@tagged("post_install", "-at_install")
class TestPurchaseBillUomValuation(AccountTestInvoicingCommon):
    def setUp(self):
        super().setUp()
        self.legacy_patchers = [
            patch.object(NativePurchaseMove, "_get_value_from_account_move", _legacy_account_value),
            patch.object(NativePurchaseMove, "_get_quantity_from_bill", None, create=True),
        ]
        for patcher in self.legacy_patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.unit = cls.env.ref("uom.product_uom_unit")
        cls.box = cls.env["uom.uom"].create({
            "name": "Regression Box (300 Tests)", "relative_factor": 300,
            "relative_uom_id": cls.unit.id,
        })
        cls.category = cls.env["product.category"].create({
            "name": "Focused Bill UoM Regression", "property_cost_method": "average",
            "property_account_expense_categ_id": cls.company_data["default_account_expense"].id,
        })
        cls.product = cls.env["product.product"].create({
            "name": "BC 0001 regression Albumin", "is_storable": True,
            "uom_id": cls.unit.id, "categ_id": cls.category.id,
            "supplier_taxes_id": [Command.clear()],
        })
        cls.warehouse = cls.env["stock.warehouse"].search([
            ("company_id", "=", cls.env.company.id),
        ], limit=1)
        if not cls.warehouse:
            cls.warehouse = cls.env["stock.warehouse"].create({
                "name": "Focused valuation tests", "code": "FVT",
                "company_id": cls.env.company.id,
            })

    def _cost(self):
        with freeze_time("2026-08-15 06:00:00"):
            self.product.standard_price = 276

    def _stock_move(self, quantity, when, outgoing=False):
        supplier = self.env.ref("stock.stock_location_suppliers")
        destination = self.env.ref("stock.stock_location_customers")
        with freeze_time(when):
            move = self.env["stock.move"].create({
                "product_id": self.product.id, "product_uom": self.unit.id,
                "product_uom_qty": quantity, "company_id": self.env.company.id,
                "location_id": self.warehouse.lot_stock_id.id if outgoing else supplier.id,
                "location_dest_id": destination.id if outgoing else self.warehouse.lot_stock_id.id,
            })
            move._action_confirm()
            move._action_assign()
            move.quantity = quantity
            move.picked = True
            move._action_done()
        return move

    def _purchase(self, uom=None, quantity=2, billed=2, price=82800, bill_date="2026-09-28"):
        uom = uom or self.box
        with freeze_time("2026-09-28 06:00:00"):
            order = self.env["purchase.order"].create({
                "partner_id": self.partner_a.id, "picking_type_id": self.warehouse.in_type_id.id,
                "order_line": [Command.create({
                    "product_id": self.product.id, "name": self.product.name,
                    "product_qty": quantity, "product_uom_id": uom.id,
                    "price_unit": price, "tax_ids": [Command.clear()],
                })],
            })
            order.button_confirm()
            move = order.picking_ids.move_ids
            move.quantity = uom._compute_quantity(quantity, self.unit, round=False)
            move.picked = True
            order.picking_ids.button_validate()
        bill = self._bill(order.order_line, billed, uom, price, bill_date)
        return order, move, bill

    def _bill(self, purchase_line, quantity, uom, price, date, move_type="in_invoice", post=True):
        with freeze_time("2026-10-08 06:00:00"):
            bill = self.env["account.move"].create({
                "move_type": move_type, "partner_id": self.partner_a.id,
                "invoice_date": date, "date": date,
                "invoice_line_ids": [Command.create({
                    "product_id": self.product.id, "purchase_line_id": purchase_line.id,
                    "product_uom_id": uom.id, "quantity": quantity, "price_unit": price,
                    "account_id": self.company_data["default_account_expense"].id,
                    "tax_ids": [Command.clear()],
                })],
            })
            if post:
                bill.action_post()
        return bill

    def _preview(self):
        return build_preview(self.env, self.env.company.id, self.env.company.name)

    def _legacy_history(self):
        self._cost()
        self._stock_move(1680, "2026-08-31 06:00:00")
        order, receipt, bill = self._purchase()
        outgoing = self._stock_move(600, "2026-09-30 06:00:00", outgoing=True)
        receipt.value = 330648
        return order, receipt, bill, outgoing

    def test_bc0001_exact_native_defect_and_corrected_receipt(self):
        self._cost()
        order, move, bill = self._purchase()
        native = NativePurchaseMove._get_value_from_account_move(move, 600)
        self.assertEqual((native["quantity"], native["value"]), (2, 165600))
        legacy = native["value"] + (600 - native["quantity"]) * 276
        self.assertEqual(legacy, 330648)
        corrected = move._get_value_from_account_move(600)
        self.assertEqual((corrected["quantity"], corrected["value"]), (600, 165600))
        self.assertEqual(move.value, 165600)
        self.assertEqual(move._get_value(at_date=fields.Datetime.to_datetime("2026-09-30 17:29:59")), 165600)
        self.assertEqual(order.order_line.price_unit, 82800)
        self.assertEqual(bill.invoice_line_ids.quantity, 2)
        self.assertEqual(move.quantity, 600)

    def test_same_unit_purchase_unchanged(self):
        self._cost()
        order, move, bill = self._purchase(self.unit, 600, 600, 276)
        self.assertEqual(move.value, 165600)
        self.assertEqual(move._get_value_from_account_move(600)["quantity"], 600)

    def test_partial_billing_only_values_unbilled_stock_at_po_price(self):
        self._cost()
        order, move, bill = self._purchase(billed=1)
        self.assertEqual(move._get_value_from_account_move(600)["quantity"], 300)
        self.assertEqual(move._get_value(), 165600)
        self.assertIn("partial billing", " ".join(self._preview()["receipts"][0]["reasons"]))

    def test_refund_quantity_converted_and_draft_ignored(self):
        self._cost()
        order, move, bill = self._purchase()
        self._bill(order.order_line, 1, self.box, 82800, "2026-09-28", "in_refund")
        self._bill(order.order_line, 50, self.box, 82800, "2026-09-28", post=False)
        data = move._get_value_from_account_move(600)
        self.assertEqual((data["quantity"], data["value"]), (300, 82800))
        self.assertEqual(move._get_value(), 165600)
        self.assertEqual(self._preview()["receipts"][0]["status"], "blocked")

    def test_future_bill_excluded_by_accounting_date(self):
        self._cost()
        order, move, bill = self._purchase(bill_date="2026-10-01")
        at_date = fields.Datetime.to_datetime("2026-09-30 17:29:59")
        data = move._get_value_from_account_move(600, at_date)
        self.assertEqual((data["quantity"], data["value"]), (0, 0))
        self.assertEqual(move._get_value(at_date=at_date), 165600)
        self.assertEqual(self._preview()["receipts"][0]["status"], "blocked")

    def test_fractional_conversion_is_not_rounded(self):
        self._cost()
        box = self.env["uom.uom"].create({
            "name": "Regression Box of 3", "relative_factor": 3,
            "relative_uom_id": self.unit.id,
        })
        order, move, bill = self._purchase(box, quantity=1, billed=0.1, price=828)
        # Exercise this override directly: the retired addon already installed
        # in THA can sit above it in the MRO and re-round billed quantities.
        data = FocusedStockMove._get_value_from_account_move(move, 3)
        self.assertAlmostEqual(data["quantity"], 0.3)
        self.assertAlmostEqual(data["value"] + (3 - data["quantity"]) * 276, 828)

    def test_split_receipts_are_explicitly_blocked(self):
        self._cost()
        order, move, bill = self._purchase()
        # A second receipt/backorder on the same PO line is not safe for automatic
        # historical repair: native valuation uses the entire PO line's bills.
        self.env["stock.move"].create({
            "product_id": self.product.id, "product_uom": self.unit.id,
            "product_uom_qty": 300, "purchase_line_id": order.order_line.id,
            "location_id": move.location_id.id, "location_dest_id": move.location_dest_id.id,
        })
        self.assertEqual(move._get_value_from_account_move(600)["quantity"], 600)
        row = self._preview()["receipts"][0]
        self.assertEqual(row["status"], "blocked")
        self.assertIn("split receipt", " ".join(row["reasons"]))

    def test_preview_apply_and_idempotence(self):
        order, receipt, bill, outgoing = self._legacy_history()
        before = (receipt.value, outgoing.value, self.product.standard_price)
        preview = self._preview()
        self.assertEqual(before, (receipt.value, outgoing.value, self.product.standard_price))
        row = preview["receipts"][0]
        self.assertEqual(row["status"], "eligible", row["reasons"])
        effect = preview["products"][0]
        self.assertEqual(effect["closing"], {"quantity": 1680, "before": 628728, "after": 463680})
        # Exercise the actual JSON review-file round trip.
        preview = json.loads(json.dumps(preview, default=str))
        result = apply_preview(self.env, preview, [receipt.id])
        self.assertEqual(receipt.value, 165600)
        self.assertEqual(outgoing.value, 165600)
        self.assertEqual(self.product.standard_price, 276)
        self.assertEqual(result["after"]["receipts"][0]["status"], "unchanged")
        self.assertFalse(any(row["status"] == "eligible" for row in self._preview()["receipts"]))
        with self.assertRaisesRegex(ValueError, "Stale preview"):
            apply_preview(self.env, preview, [receipt.id])
        self.assertEqual(receipt.value, 165600)

    def test_stale_and_tampered_preview_rejected(self):
        order, receipt, bill, outgoing = self._legacy_history()
        preview = self._preview()
        tampered = copy.deepcopy(preview)
        tampered["receipts"][0]["proposed_value"] = 1
        with self.assertRaisesRegex(ValueError, "proposals differ"):
            apply_preview(self.env, tampered, [receipt.id])
        receipt.value = 330649
        with self.assertRaisesRegex(ValueError, "Stale preview"):
            apply_preview(self.env, preview, [receipt.id])
        self.assertEqual(receipt.value, 330649)

    def test_downstream_outgoing_valuation_repaired(self):
        order, receipt, bill, outgoing = self._legacy_history()
        outgoing.value = 209036.84
        preview = self._preview()
        self.assertEqual(preview["receipts"][0]["status"], "eligible")
        apply_preview(self.env, preview, [receipt.id])
        self.assertEqual(outgoing.value, 165600)

    def test_manual_receipt_override_blocked(self):
        order, receipt, bill, outgoing = self._legacy_history()
        self.env["product.value"].create({
            "move_id": receipt.id, "company_id": self.env.company.id, "value": 330648,
        })
        self.assertEqual(self._preview()["receipts"][0]["status"], "blocked")

    def test_product_cost_reset_preserved(self):
        order, receipt, bill, outgoing = self._legacy_history()
        with freeze_time("2026-10-10 06:00:00"):
            self.product.standard_price = 300
        # Creating a manual reset can revalue remaining receipts natively;
        # restore this defect as the explicit audit input.
        receipt.value = 330648
        preview = self._preview()
        manual = self.env["product.value"].search([("product_id", "=", self.product.id)])
        values_before = manual.read(["value", "date", "write_date"])
        apply_preview(self.env, preview, [receipt.id])
        self.assertEqual(self.product.standard_price, 300)
        self.assertEqual(manual.read(["value", "date", "write_date"]), values_before)

    def test_apply_failure_rolls_back_all_writes(self):
        order, receipt, bill, outgoing = self._legacy_history()
        outgoing.value = 209036.84
        preview = self._preview()
        original_write = type(outgoing).write

        def fail_after_receipt(records, vals):
            if outgoing.id in records.ids and "value" in vals:
                self.assertEqual(receipt.value, 165600)
                raise ValueError("forced failure")
            return original_write(records, vals)

        with patch.object(type(outgoing), "write", fail_after_receipt):
            with self.assertRaisesRegex(ValueError, "forced failure"):
                apply_preview(self.env, preview, [receipt.id])
        self.assertEqual(receipt.value, 330648)
        self.assertEqual(outgoing.value, 209036.84)

    def test_company_identity_and_unapproved_ids_rejected(self):
        order, receipt, bill, outgoing = self._legacy_history()
        with self.assertRaisesRegex(ValueError, "Company ID/name"):
            build_preview(self.env, self.env.company.id, "Wrong company")
        with self.assertRaisesRegex(ValueError, "explicit eligible"):
            apply_preview(self.env, self._preview(), [outgoing.id])

    def test_linked_accounting_entry_blocks_product_repair(self):
        order, receipt, bill, outgoing = self._legacy_history()
        receipt.account_move_id = bill
        row = self._preview()["receipts"][0]
        self.assertEqual(row["status"], "blocked")
        self.assertIn("linked accounting entry", " ".join(row["reasons"]))

    def test_new_activity_invalidates_preview(self):
        order, receipt, bill, outgoing = self._legacy_history()
        preview = self._preview()
        self._stock_move(1, "2026-10-02 06:00:00", outgoing=True)
        with self.assertRaisesRegex(ValueError, "Stale preview"):
            apply_preview(self.env, preview, [receipt.id])

    def test_company_month_uses_rangoon_boundaries(self):
        self._cost()
        order, receipt, bill = self._purchase()
        receipt.date = "2026-08-31 18:00:00"  # September 1 in Rangoon.
        self.assertEqual(len(self._preview()["receipts"]), 1)
        receipt.date = "2026-09-30 18:00:00"  # October 1 in Rangoon.
        self.assertFalse(self._preview()["receipts"])

    def test_upstream_monetary_additions_preserved(self):
        self._cost()
        order, receipt, bill = self._purchase()
        with patch.object(NativePurchaseMove, "_get_value_from_account_move", return_value={
            "quantity": 2, "value": 165700, "description": "upstream addition",
        }):
            data = receipt._get_value_from_account_move(600)
        self.assertEqual((data["quantity"], data["value"]), (600, 165700))

    def test_no_purchase_line_preserves_upstream_result(self):
        self._cost()
        receipt = self._stock_move(10, "2026-08-31 06:00:00")
        self.assertEqual(receipt._get_value_from_account_move(10), {
            "value": 0, "quantity": 0, "description": False,
        })

    def test_foreign_currency_requires_manual_review(self):
        order, receipt, bill, outgoing = self._legacy_history()
        currency = self.env.ref("base.EUR")
        if currency == self.env.company.currency_id:
            currency = self.env.ref("base.USD")
        currency.active = True
        order.currency_id = currency
        row = self._preview()["receipts"][0]
        self.assertEqual(row["status"], "blocked")
        self.assertIn("foreign currency", row["reasons"])

    def test_complete_product_group_required(self):
        order, receipt, bill, outgoing = self._legacy_history()
        second_order, second_receipt, second_bill = self._purchase()
        second_receipt.value = 330648
        preview = self._preview()
        self.assertEqual(sum(row["status"] == "eligible" for row in preview["receipts"]), 2)
        with self.assertRaisesRegex(ValueError, "all eligible September receipts"):
            apply_preview(self.env, preview, [receipt.id])

    def test_preview_does_not_write_business_records(self):
        order, receipt, bill, outgoing = self._legacy_history()
        # Any ORM business write would fail this preview-only check.
        with patch.object(type(receipt), "write", side_effect=AssertionError("stock write")), \
             patch.object(type(self.product), "write", side_effect=AssertionError("product write")), \
             patch.object(type(bill), "write", side_effect=AssertionError("bill write")):
            self.assertEqual(self._preview()["receipts"][0]["status"], "eligible")

    def test_newer_native_allocation_passes_through_unchanged(self):
        for patcher in reversed(self.legacy_patchers):
            patcher.stop()
        if not callable(getattr(NativePurchaseMove, "_get_quantity_from_bill", None)):
            self.skipTest("Installed core is the older build without native allocation")
        self._cost()
        order, receipt, bill = self._purchase()
        native = NativePurchaseMove._get_value_from_account_move(receipt, 300)
        self.assertEqual((native["quantity"], native["value"]), (300, 82800))
        parent = super(FocusedStockMove, receipt)._get_value_from_account_move(300)
        self.assertEqual(FocusedStockMove._get_value_from_account_move(receipt, 300), parent)

    def test_zero_stock_keeps_last_native_unit_cost(self):
        self._cost()
        order, receipt, bill = self._purchase()
        outgoing = self._stock_move(600, "2026-09-30 06:00:00", outgoing=True)
        receipt.value = 330648
        preview = self._preview()
        self.assertEqual(preview["products"][0]["current_quantity"], 0)
        self.assertEqual(preview["products"][0]["current_cost_after"], 276)
        apply_preview(self.env, preview, [receipt.id])
        self.assertEqual(receipt.value, 165600)
        self.assertEqual(outgoing.value, 165600)
        self.assertEqual(self.product.standard_price, 276)
