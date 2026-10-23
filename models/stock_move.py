from odoo import _, fields, models


class StockMove(models.Model):
    _inherit = "stock.move"

    def _get_value_from_account_move(self, quantity, at_date=None):
        data = super()._get_value_from_account_move(quantity, at_date=at_date)
        self.ensure_one()
        if not self.purchase_line_id:
            return data
        # Newer Odoo 19 builds already normalize and allocate bill quantities.
        # Replacing their allocated quantity with the whole PO-line quantity
        # would break partial receipts. Leave that native implementation intact.
        if callable(getattr(self, "_get_quantity_from_bill", None)) and callable(
            getattr(self, "_get_value_from_bill", None)):
            return data

        billed_quantity = 0.0
        bills = self.env["account.move"]
        for line in self.purchase_line_id.invoice_lines:
            if line.move_id.state != "posted":
                continue
            if at_date and fields.Datetime.to_datetime(line.date) > at_date:
                continue
            if line.move_id.move_type not in ("in_invoice", "in_refund"):
                continue
            # _get_valued_qty() consumes move-line quantities in the move's UoM.
            # Do not subtract boxes from tests, or round fractional billed units.
            uom = line.product_uom_id or line.product_id.uom_id
            converted = uom._compute_quantity(line.quantity, self.product_uom, round=False)
            billed_quantity += converted * (-1 if line.move_id.move_type == "in_refund" else 1)
            bills |= line.move_id

        # Preserve native monetary valuation (including other addons' additions).
        data["quantity"] = billed_quantity
        data["description"] = _(
            "%(value)s for %(quantity)s %(unit)s from %(bills)s",
            value=data["value"], quantity=billed_quantity,
            unit=self.product_uom.name, bills=", ".join(bills.mapped("name")),
        )
        return data
