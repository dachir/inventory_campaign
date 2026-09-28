from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import flt


ISSUE_STOCK_ENTRY_TYPE = "Inventory Count Issue"
RECEIPT_STOCK_ENTRY_TYPE = "Inventory Count Receipt"


class InventoryCountSession(Document):
    """Manual inventory count session.

    On every save, the document calculates the quantity/value variance of each
    counted row and the signed total variance on the parent document.

    On submit, at most two Stock Entries are generated and submitted:
    - Inventory Count Issue   -> all negative quantity differences
    - Inventory Count Receipt -> all positive quantity differences

    Quality Status is carried as-is from the inventory snapshot to the generated
    Stock Entry Detail. No status value (including Q) receives special handling
    in this controller.
    """

    def validate(self):
        self.calculate_inventory_differences()

    def before_submit(self):
        # validate() has already recalculated the values before submit, but keep
        # the business validations explicit here.
        self.validate_all_lines_are_counted()
        self.validate_stock_entry_types()
        self.validate_adjustment_rows()

    def on_submit(self):
        issue_rows = []
        receipt_rows = []

        for row in self.get("table_uawl") or []:
            difference_qty = flt(row.quantity_difference)

            if difference_qty < 0:
                issue_rows.append(row)
            elif difference_qty > 0:
                receipt_rows.append(row)

        if issue_rows:
            self.create_inventory_stock_entry(
                stock_entry_type=ISSUE_STOCK_ENTRY_TYPE,
                expected_purpose="Material Issue",
                rows=issue_rows,
            )

        if receipt_rows:
            self.create_inventory_stock_entry(
                stock_entry_type=RECEIPT_STOCK_ENTRY_TYPE,
                expected_purpose="Material Receipt",
                rows=receipt_rows,
            )

    # ------------------------------------------------------------------
    # Calculations
    # ------------------------------------------------------------------

    def calculate_inventory_differences(self):
        """Calculate every line variance and the signed parent total.

        Unit convention:
            physical_count       = physical quantity entered in CT
            qty_in_ct            = ERP theoretical quantity in CT
            valuation_rate       = stock_valuation / qty_in_ct (value per CT)

        Formulas:
            quantity_difference  = physical_count - qty_in_ct
            physical_stock_value = physical_count * valuation_rate
            difference_amount    = physical_stock_value - stock_valuation

        Stock Entries are still generated in the Item stock UOM. Therefore a
        CT variance is converted back to stock UOM with ct_conversion_factor.

        A line whose physical_count has not been entered yet is not treated as
        zero during a draft save. Its calculated values are reset to zero so a
        partially entered session can safely be saved.
        """

        total_difference_amount = 0.0

        for row in self.get("table_uawl") or []:
            if self._is_blank(row.get("physical_count")):
                row.quantity_difference = 0
                row.physical_stock_value = 0
                row.difference_amount = 0
                continue

            physical_count = flt(row.physical_count)
            qty_in_ct = flt(row.qty_in_ct)
            stock_valuation = flt(row.stock_valuation)

            # valuation_rate is intentionally a CT rate, not a stock-UOM rate.
            # Normal rule: stock_valuation / qty_in_ct. When theoretical CT qty
            # is zero the division is impossible; use the current item/warehouse
            # valuation converted to CT as a fallback so a discovered surplus can
            # still be valued.
            valuation_rate = self._get_ct_valuation_rate(row)
            row.valuation_rate = flt(valuation_rate)

            row.quantity_difference = flt(physical_count - qty_in_ct)
            row.physical_stock_value = flt(physical_count * valuation_rate)
            row.difference_amount = flt(
                row.physical_stock_value - stock_valuation
            )

            total_difference_amount += flt(row.difference_amount)

        self.difference_amount = flt(total_difference_amount)

    # ------------------------------------------------------------------
    # Validations
    # ------------------------------------------------------------------

    def validate_all_lines_are_counted(self):
        missing_rows = []

        for row in self.get("table_uawl") or []:
            if self._is_blank(row.get("physical_count")):
                missing_rows.append(str(row.idx))

        if missing_rows:
            frappe.throw(
                "Physical Count is required before submit for row(s): {0}.".format(
                    ", ".join(missing_rows)
                )
            )

    def validate_stock_entry_types(self):
        self._get_and_validate_purpose(
            ISSUE_STOCK_ENTRY_TYPE, expected_purpose="Material Issue"
        )
        self._get_and_validate_purpose(
            RECEIPT_STOCK_ENTRY_TYPE, expected_purpose="Material Receipt"
        )

    def validate_adjustment_rows(self):
        """Validate data required to create safe inventory Stock Entries.

        Batch/serial-controlled items are deliberately blocked for now because
        the current Inventory Count Session Detail is aggregated by
        item/warehouse/quality status and does not identify the batch or serial
        numbers that must be issued/received.
        """

        errors = []

        for row in self.get("table_uawl") or []:
            difference_qty = flt(row.quantity_difference)
            if not difference_qty:
                continue

            if not row.item_code:
                errors.append(f"Row {row.idx}: Item Code is required.")
                continue

            if not row.warehouse:
                errors.append(f"Row {row.idx}: Warehouse is required.")

            if not row.quality_status:
                errors.append(f"Row {row.idx}: Quality Status is required.")

            if flt(row.ct_conversion_factor) <= 0:
                errors.append(
                    f"Row {row.idx} ({row.item_code}): CT Conversion Factor must be greater than zero."
                )

            item_flags = frappe.db.get_value(
                "Item",
                row.item_code,
                ["has_batch_no", "has_serial_no"],
                as_dict=True,
            ) or {}

            if item_flags.get("has_batch_no") or item_flags.get("has_serial_no"):
                controlled_by = []
                if item_flags.get("has_batch_no"):
                    controlled_by.append("batch")
                if item_flags.get("has_serial_no"):
                    controlled_by.append("serial number")

                errors.append(
                    "Row {0} ({1}): adjustment cannot be generated yet because "
                    "the Item is controlled by {2}.".format(
                        row.idx,
                        row.item_code,
                        " and ".join(controlled_by),
                    )
                )

            if difference_qty > 0 and self._get_receipt_rate(row) <= 0:
                errors.append(
                    "Row {0} ({1}): a positive inventory difference requires "
                    "a valuation rate greater than zero for the receipt.".format(
                        row.idx, row.item_code
                    )
                )

        if errors:
            frappe.throw("<br>".join(errors))

    # ------------------------------------------------------------------
    # Stock Entry generation
    # ------------------------------------------------------------------

    def create_inventory_stock_entry(
        self,
        stock_entry_type: str,
        expected_purpose: str,
        rows: list,
    ):
        purpose = self._get_and_validate_purpose(
            stock_entry_type, expected_purpose=expected_purpose
        )
        company = self._get_company()

        stock_entry = frappe.new_doc("Stock Entry")
        stock_entry.company = company
        stock_entry.stock_entry_type = stock_entry_type
        stock_entry.purpose = purpose
        stock_entry.posting_date = self.inventory_date
        stock_entry.remarks = (
            f"Generated automatically from Inventory Count Session {self.name} "
            f"for Inventory Campaign {self.inventory_campaign}."
        )

        stock_entry_meta = frappe.get_meta("Stock Entry")
        detail_meta = frappe.get_meta("Stock Entry Detail")

        if self.branch and stock_entry_meta.has_field("branch"):
            stock_entry.branch = self.branch

        for row in rows:
            # quantity_difference is expressed in CT. Stock Entry is deliberately
            # posted in the Item stock UOM, so convert the variance back to stock
            # units before creating the movement.
            difference_qty_ct = flt(row.quantity_difference)
            conversion_factor = self._get_ct_conversion_factor(row)
            qty_stock_uom = abs(difference_qty_ct) * conversion_factor

            item = {
                "item_code": row.item_code,
                "qty": qty_stock_uom,
                "uom": row.stock_uom,
                "conversion_factor": 1,
            }

            if expected_purpose == "Material Issue":
                item["s_warehouse"] = row.warehouse
            else:
                item["t_warehouse"] = row.warehouse
                # For an inventory surplus, ERPNext needs an incoming value.
                item["basic_rate"] = self._get_receipt_rate(row)

            # Preserve the Quality Status exactly as counted.
            # No special treatment is applied for Q or any other status.
            if detail_meta.has_field("quality_status"):
                item["quality_status"] = row.quality_status

            if self.branch and detail_meta.has_field("branch"):
                item["branch"] = self.branch

            stock_entry.append("items", item)

        stock_entry.insert()
        stock_entry.submit()

        # Keep an auditable trail even without adding extra Link fields to the
        # Inventory Count Session DocType yet.
        self.add_comment(
            "Info",
            f"Created and submitted Stock Entry {stock_entry.name} "
            f"({stock_entry_type}).",
        )

        return stock_entry.name

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_company(self) -> str:
        if not self.inventory_campaign:
            frappe.throw("Inventory Campaign is required.")

        company = frappe.db.get_value(
            "Inventory Campaign", self.inventory_campaign, "company"
        )
        if not company:
            frappe.throw(
                f"Inventory Campaign {self.inventory_campaign} has no Company."
            )
        return company

    @staticmethod
    def _is_blank(value) -> bool:
        return value is None or value == ""

    @staticmethod
    def _get_and_validate_purpose(
        stock_entry_type: str, expected_purpose: str
    ) -> str:
        purpose = frappe.db.get_value(
            "Stock Entry Type", stock_entry_type, "purpose"
        )

        if not purpose:
            frappe.throw(
                f"Stock Entry Type {stock_entry_type} does not exist or has no purpose."
            )

        if purpose != expected_purpose:
            frappe.throw(
                "Stock Entry Type {0} must have purpose {1}, current purpose is {2}.".format(
                    stock_entry_type, expected_purpose, purpose
                )
            )

        return purpose

    @staticmethod
    def _get_ct_conversion_factor(row) -> float:
        factor = flt(row.ct_conversion_factor)
        return factor if factor > 0 else 1.0

    @staticmethod
    def _get_base_uom_fallback_rate(row) -> float:
        """Return a valuation rate expressed in the Item stock UOM."""

        rate = flt(
            frappe.db.get_value(
                "Bin",
                {"item_code": row.item_code, "warehouse": row.warehouse},
                "valuation_rate",
            )
        )
        if rate > 0:
            return rate

        item_rates = frappe.db.get_value(
            "Item",
            row.item_code,
            ["valuation_rate", "last_purchase_rate"],
            as_dict=True,
        ) or {}

        return flt(item_rates.get("valuation_rate")) or flt(
            item_rates.get("last_purchase_rate")
        )

    def _get_ct_valuation_rate(self, row) -> float:
        """Return the inventory valuation rate expressed per CT.

        Primary rule required by Inventory Count:
            CT valuation rate = stock_valuation / qty_in_ct

        If qty_in_ct is zero, the rate cannot be derived from the snapshot. In
        that edge case, fall back to the current stock-UOM valuation and convert
        it to CT so a physically found surplus can still receive a value.
        """

        qty_in_ct = flt(row.qty_in_ct)
        stock_valuation = flt(row.stock_valuation)

        if qty_in_ct:
            return flt(stock_valuation / qty_in_ct)

        base_rate = self._get_base_uom_fallback_rate(row)
        if base_rate > 0:
            return flt(base_rate * self._get_ct_conversion_factor(row))

        return 0.0

    def _get_receipt_rate(self, row) -> float:
        """Return incoming rate in stock UOM for the generated Stock Receipt.

        ``row.valuation_rate`` is stored per CT, while Stock Entry is posted in
        stock UOM. Convert the CT rate back to a stock-UOM rate.
        """

        ct_rate = flt(row.valuation_rate)
        if ct_rate > 0:
            return flt(ct_rate / self._get_ct_conversion_factor(row))

        return self._get_base_uom_fallback_rate(row)

@frappe.whitelist()
def get_inventory_snapshot(
    inventory_campaign: str,
    inventory_date: str,
    branch: str,
    warehouses=None,
):
    """Return the ERP stock snapshot used to initialise a count session.

    The snapshot is filtered by:
      - company from Inventory Campaign
      - Inventory Count Session branch
      - exact warehouses selected in ``inventory_count_warehouses``
      - Stock Ledger entries posted up to ``inventory_date``

    This method only reads data. Clearing/replacing ``table_uawl`` is done by
    the client after the user explicitly confirms the destructive action.
    """

    if not inventory_campaign:
        frappe.throw("Inventory Campaign is required.")
    if not inventory_date:
        frappe.throw("Inventory Date is required.")
    if not branch:
        frappe.throw("Branch is required.")

    warehouses = _normalise_warehouse_list(warehouses)
    if not warehouses:
        frappe.throw("Select at least one Warehouse before initialising the session.")

    company = frappe.db.get_value(
        "Inventory Campaign", inventory_campaign, "company"
    )
    if not company:
        frappe.throw(
            f"Inventory Campaign {inventory_campaign} has no Company."
        )

    placeholders = ", ".join(["%s"] * len(warehouses))

    sql_query = f"""
        WITH WarehouseStockAccount AS (
            SELECT
                w.name AS warehouse,
                w.branch AS branch,
                CASE
                    WHEN w.account IS NOT NULL AND w.account != ''
                    THEN w.account
                    ELSE pw.account
                END AS stock_account
            FROM `tabWarehouse` w
            LEFT JOIN `tabWarehouse` pw
                ON w.parent_warehouse = pw.name
            WHERE w.company = %s
        )

        SELECT
            i.item_group AS item_group,
            sle.item_code AS item_code,
            i.item_name AS item_name,
            i.stock_uom AS stock_uom,
            sle.warehouse AS warehouse,
            COALESCE(NULLIF(sle.quality_status, ''), 'A') AS quality_status,
            ws.stock_account AS stock_account,
            SUM(sle.actual_qty) AS stock_balance,
            COALESCE(NULLIF(ucd.conversion_factor, 0), 1) AS ct_conversion_factor,
            (
                SUM(sle.actual_qty)
                / COALESCE(NULLIF(ucd.conversion_factor, 0), 1)
            ) AS qty_in_ct,
            COALESCE(
                SUM(sle.stock_value_difference)
                / NULLIF(
                    SUM(sle.actual_qty)
                    / COALESCE(NULLIF(ucd.conversion_factor, 0), 1),
                    0
                ),
                0
            ) AS valuation_rate,
            SUM(sle.stock_value_difference) AS stock_valuation
        FROM `tabStock Ledger Entry` sle
        INNER JOIN `tabItem` i
            ON sle.item_code = i.item_code
        LEFT JOIN WarehouseStockAccount ws
            ON sle.warehouse = ws.warehouse
        LEFT JOIN `tabUOM Conversion Detail` ucd
            ON sle.item_code = ucd.parent
            AND ucd.parenttype = 'Item'
            AND ucd.parentfield = 'uoms'
            AND ucd.uom = 'CT'
        WHERE
            sle.company = %s
            AND sle.posting_date <= %s
            AND ws.branch = %s
            AND sle.warehouse IN ({placeholders})
            AND sle.docstatus = 1
            AND sle.is_cancelled = 0
        GROUP BY
            i.item_group,
            sle.item_code,
            i.item_name,
            i.stock_uom,
            sle.warehouse,
            COALESCE(NULLIF(sle.quality_status, ''), 'A'),
            ws.stock_account,
            ucd.conversion_factor
        ORDER BY
            sle.warehouse,
            ws.stock_account,
            sle.item_code,
            quality_status
    """

    params = [
        company,           # CTE company
        company,           # SLE company
        inventory_date,
        branch,
        *warehouses,
    ]

    rows = frappe.db.sql(sql_query, params, as_dict=True)

    numeric_fields = (
        "stock_balance",
        "ct_conversion_factor",
        "qty_in_ct",
        "valuation_rate",
        "stock_valuation",
    )

    for row in rows:
        for fieldname in numeric_fields:
            row[fieldname] = flt(row.get(fieldname))

    return rows


def _normalise_warehouse_list(warehouses) -> list[str]:
    """Normalise a JSON/list payload of Warehouse names from the client."""

    if not warehouses:
        return []

    if isinstance(warehouses, str):
        try:
            warehouses = frappe.parse_json(warehouses)
        except Exception:
            warehouses = [warehouses]

    if isinstance(warehouses, dict):
        warehouses = list(warehouses.values())

    result = []
    seen = set()

    for warehouse in warehouses or []:
        # Accept either a plain Warehouse name or a child-row dictionary.
        if isinstance(warehouse, dict):
            value = warehouse.get("warehouse") or warehouse.get("value")
        else:
            value = warehouse

        value = str(value or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)

    return result

