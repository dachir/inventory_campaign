from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate


ISSUE_STOCK_ENTRY_TYPE = "Inventory Count Issue"
RECEIPT_STOCK_ENTRY_TYPE = "Inventory Count Receipt"


class InventoryCountSession(Document):
    """Manual inventory count session.

    On every save, the document calculates the quantity/value variance of each
    counted row and the signed total variance on the parent document.

    On submit, at most two Stock Entries are generated in Draft:
    - Inventory Count Issue   -> all negative quantity differences
    - Inventory Count Receipt -> all positive quantity differences

    Quality Status is carried as-is from the inventory snapshot to the generated
    Stock Entry Detail. No status value (including Q) receives special handling
    in this controller.
    """

    def validate(self):
        # A Count Session must stay inside its campaign period, and a Warehouse
        # may belong to only one active Count Session during that period.
        self.validate_campaign_period()
        self.validate_warehouse_period_exclusivity()
        self.calculate_inventory_differences()

    def before_submit(self):
        # Session-level rule only: every row must have been physically counted.
        # Once Stock Entry creation starts, ERPNext standard validations are the
        # sole authority for the Stock Entry and its items.
        self.validate_all_lines_are_counted()
        self.validate_manual_valuation_rates()

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
                purpose="Material Issue",
                rows=issue_rows,
            )

        if receipt_rows:
            self.create_inventory_stock_entry(
                stock_entry_type=RECEIPT_STOCK_ENTRY_TYPE,
                purpose="Material Receipt",
                rows=receipt_rows,
            )

    # ------------------------------------------------------------------
    # Calculations
    # ------------------------------------------------------------------

    def calculate_inventory_differences(self):
        """Recalculate valuation and inventory variances.

        ``manual_valuation_required`` is the authoritative flag for rows whose
        theoretical quantity is zero and for which no historical valuation rate
        exists in Stock Ledger Entry.

        Rules:
        - qty_in_ct != 0: valuation_rate = stock_valuation / qty_in_ct and the
          manual flag is cleared.
        - qty_in_ct == 0 + historical SLE rate found: use the latest historical
          rate converted from stock UOM to CT and clear the manual flag.
        - qty_in_ct == 0 + no historical rate: set the manual flag and preserve
          the user's valuation_rate entry instead of overwriting it.
        """

        rows = self.get("table_uawl") or []
        company = self._get_company() if rows else None

        # Resolve historical rates in one query, rather than one query per row.
        zero_qty_items = {
            row.item_code
            for row in rows
            if row.item_code
            and not cint(row.get("manual_entry"))
            and flt(row.qty_in_ct) == 0
            and flt(row.valuation_rate) <= 0
            and not cint(row.get("manual_valuation_required"))
        }
        historical_rates = _get_latest_sle_valuation_rates(
            company=company,
            inventory_date=self.inventory_date,
            item_codes=zero_qty_items,
        ) if zero_qty_items else {}

        total_difference_amount = 0.0

        for row in rows:
            qty_in_ct = flt(row.qty_in_ct)
            stock_valuation = flt(row.stock_valuation)

            # A Manual Entry represents stock physically found in a Warehouse but
            # absent from the ERP snapshot. Its theoretical quantity/value are zero,
            # while Physical Stock Value is entered by the counter.
            if cint(row.get("manual_entry")):
                row.stock_balance = 0
                row.qty_in_ct = 0
                row.stock_valuation = 0
                row.manual_valuation_required = 0

                if self._is_blank(row.get("physical_count")):
                    row.quantity_difference = 0
                    row.valuation_rate = 0
                    row.difference_amount = 0
                    continue

                physical_count = flt(row.physical_count)
                physical_stock_value = flt(row.physical_stock_value)

                row.quantity_difference = physical_count
                row.valuation_rate = flt(
                    physical_stock_value / physical_count
                ) if physical_count else 0
                row.difference_amount = physical_stock_value
                total_difference_amount += flt(row.difference_amount)
                continue

            if qty_in_ct != 0:
                valuation_rate = flt(stock_valuation / qty_in_ct)
                row.manual_valuation_required = 0
            else:
                current_rate = flt(row.valuation_rate)

                if cint(row.get("manual_valuation_required")):
                    # This is a genuinely manual rate. Never overwrite it on Save.
                    valuation_rate = current_rate
                elif current_rate > 0:
                    # The snapshot already supplied the Item + Warehouse valuation
                    # rate. Keep it even when this specific Quality Status quantity
                    # is zero so valuation stays independent of Quality Status.
                    valuation_rate = current_rate
                    row.manual_valuation_required = 0
                else:
                    base_rate = flt(historical_rates.get(row.item_code))
                    if base_rate > 0:
                        valuation_rate = flt(
                            base_rate * self._get_ct_conversion_factor(row)
                        )
                        row.manual_valuation_required = 0
                    else:
                        # No warehouse or historical valuation exists. The checkbox
                        # becomes the explicit state that unlocks valuation_rate.
                        row.manual_valuation_required = 1
                        valuation_rate = current_rate

            row.valuation_rate = valuation_rate

            # A blank count is neutral: it does not create a variance and keeps the
            # physical value aligned with the theoretical book value.
            if self._is_blank(row.get("physical_count")):
                row.quantity_difference = 0
                row.physical_stock_value = stock_valuation
                row.difference_amount = 0
                continue

            physical_count = flt(row.physical_count)
            row.quantity_difference = flt(physical_count - qty_in_ct)

            # Keep the initialized state exactly neutral even when qty_in_ct is zero
            # but a residual stock valuation exists in the ledger.
            row.difference_amount = flt(
                row.quantity_difference * valuation_rate
            )
            row.physical_stock_value = flt(
                stock_valuation + row.difference_amount
            )

            total_difference_amount += flt(row.difference_amount)

        self.difference_amount = flt(total_difference_amount)

    # ------------------------------------------------------------------
    # Validations
    # ------------------------------------------------------------------

    def validate_campaign_period(self):
        _validate_inventory_date_in_campaign_period(
            inventory_campaign=self.inventory_campaign,
            inventory_date=self.inventory_date,
        )

    def validate_warehouse_period_exclusivity(self):
        warehouses = _get_selected_session_warehouses(self)
        if not warehouses:
            return

        _validate_warehouse_period_exclusivity(
            inventory_campaign=self.inventory_campaign,
            warehouses=warehouses,
            current_session=self.name,
            lock_warehouses=True,
        )

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

    def validate_manual_valuation_rates(self):
        """Require a manual CT rate only when the checkbox says it is needed."""

        missing = []
        for row in self.get("table_uawl") or []:
            if (
                cint(row.get("manual_valuation_required"))
                and flt(row.quantity_difference) > 0
                and flt(row.valuation_rate) <= 0
            ):
                missing.append(
                    f"Row {row.idx} ({row.item_code})"
                )

        if missing:
            frappe.throw(
                "Valuation Rate / CT must be entered manually for:<br>"
                + "<br>".join(missing)
            )

    # ------------------------------------------------------------------
    # Stock Entry generation
    # ------------------------------------------------------------------

    def create_inventory_stock_entry(
        self,
        stock_entry_type: str,
        purpose: str,
        rows: list,
    ):
        """Create one Draft Stock Entry and let ERPNext validate it normally.

        No custom Stock Entry validation is performed here. We only map the
        inventory-count data to a Stock Entry document and call ``insert()``.
        From that point onward, all Link, warehouse, batch/serial, valuation,
        accounting, mandatory-field and Stock Entry Type checks belong to
        ERPNext standard logic.
        """
        company = self._get_company()

        stock_entry = frappe.new_doc("Stock Entry")
        stock_entry.company = company
        stock_entry.stock_entry_type = stock_entry_type
        stock_entry.purpose = purpose
        stock_entry.posting_date = self.inventory_date

        # Traceability only; no custom validation around this field.
        stock_entry.custom_inventory_count_session = self.name
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

            if purpose == "Material Issue":
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

        # Create the adjustment Stock Entry in Draft only.
        # A stock manager can review it before submitting the actual stock movement.
        stock_entry.insert()

        # Keep an auditable trail even without adding extra Link fields to the
        # Inventory Count Session DocType yet.
        self.add_comment(
            "Info",
            f"Created Draft Stock Entry {stock_entry.name} "
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
    def _get_ct_conversion_factor(row) -> float:
        factor = flt(row.ct_conversion_factor)
        return factor if factor > 0 else 1.0

    def _get_receipt_rate(self, row) -> float:
        """Return the incoming valuation rate in the Item stock UOM.

        The Inventory Count row stores valuation_rate per CT. Stock Entry is
        created in stock UOM, so only a unit conversion is needed here. There is
        deliberately no additional custom fallback at Stock Entry creation time.
        """

        ct_rate = flt(row.valuation_rate)
        return flt(ct_rate / self._get_ct_conversion_factor(row)) if ct_rate else 0.0

@frappe.whitelist()
def get_inventory_snapshot(
    inventory_campaign: str,
    inventory_date: str,
    branch: str,
    warehouses=None,
    inventory_count_session: str | None = None,
):
    """Return the ERP stock snapshot used to initialise a count session.

    The snapshot is filtered by:
      - company from Inventory Campaign
      - Inventory Count Session branch
      - exact warehouses selected in ``inventory_count_warehouses``
      - Stock Ledger entries posted up to ``inventory_date``

    This method only reads data. Initialization and value recalculation both
    use this same snapshot source so the ERP theoretical values stay consistent.
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

    # Validate before running the snapshot query so Initialize Inventory never
    # clears/reloads a session with a Warehouse already reserved by another
    # Count Session in the same campaign period.
    _validate_inventory_date_in_campaign_period(
        inventory_campaign=inventory_campaign,
        inventory_date=inventory_date,
    )
    _validate_warehouse_period_exclusivity(
        inventory_campaign=inventory_campaign,
        warehouses=warehouses,
        current_session=inventory_count_session,
    )

    company = frappe.db.get_value(
        "Inventory Campaign", inventory_campaign, "company"
    )
    if not company:
        frappe.throw(
            f"Inventory Campaign {inventory_campaign} has no Company."
        )

    return _query_inventory_snapshot(
        company=company,
        inventory_date=inventory_date,
        branch=branch,
        warehouses=warehouses,
    )


def _query_inventory_snapshot(
    company: str,
    inventory_date: str,
    branch: str,
    warehouses: list[str],
):
    """Run the single authoritative inventory snapshot query.

    Both Initialize Inventory and Recalculate Values consume the result of this
    query through ``get_inventory_snapshot``. Keep quantity, valuation and
    Quality Status snapshot logic in this one place.
    """

    placeholders = ", ".join(["%s"] * len(warehouses))

    sql_query = f"""
    WITH

    /* =========================================================
    1. Warehouses réellement concernés
    ========================================================= */
    WarehouseScope AS (
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
            ON pw.name = w.parent_warehouse

        WHERE
            w.company = %s
            AND w.branch = %s
            AND w.name IN ({placeholders})
    ),


    /* =========================================================
    2. Quantité par Item + Warehouse + Quality Status

    quality_status affecte uniquement la quantité.
    ========================================================= */
    QualityStock AS (
        SELECT
            sle.item_code,
            sle.warehouse,

            COALESCE(
                NULLIF(sle.quality_status, ''),
                'A'
            ) AS quality_status,

            SUM(sle.actual_qty) AS stock_balance

        FROM `tabStock Ledger Entry` sle

        INNER JOIN WarehouseScope ws
            ON ws.warehouse = sle.warehouse

        WHERE
            sle.company = %s
            AND sle.posting_date <= %s
            AND sle.docstatus = 1
            AND sle.is_cancelled = 0

        GROUP BY
            sle.item_code,
            sle.warehouse,
            COALESCE(NULLIF(sle.quality_status, ''), 'A')
    ),


    /* =========================================================
    3. Dernière SLE par Item + Warehouse

    Pas de quality_status dans le PARTITION BY.
    ========================================================= */
    LastSLE AS (
        SELECT
            x.item_code,
            x.warehouse,
            x.qty_after_transaction AS warehouse_stock_qty,
            x.stock_value AS warehouse_stock_value,
            x.valuation_rate AS last_valuation_rate

        FROM (
            SELECT
                sle.item_code,
                sle.warehouse,
                sle.qty_after_transaction,
                sle.stock_value,
                sle.valuation_rate,

                ROW_NUMBER() OVER (
                    PARTITION BY
                        sle.item_code,
                        sle.warehouse
                    ORDER BY
                        sle.posting_date DESC,
                        sle.posting_time DESC,
                        sle.creation DESC,
                        sle.name DESC
                ) AS rn

            FROM `tabStock Ledger Entry` sle

            INNER JOIN WarehouseScope ws
                ON ws.warehouse = sle.warehouse

            WHERE
                sle.company = %s
                AND sle.posting_date <= %s
                AND sle.docstatus = 1
                AND sle.is_cancelled = 0
        ) x

        WHERE x.rn = 1
    ),


    /* =========================================================
    4. Un seul taux de valorisation Item + Warehouse

    Complètement indépendant du Quality Status.
    ========================================================= */
    WarehouseValuation AS (
        SELECT
            item_code,
            warehouse,
            warehouse_stock_qty,
            warehouse_stock_value,

            CASE
                WHEN COALESCE(warehouse_stock_qty, 0) <> 0
                THEN warehouse_stock_value / warehouse_stock_qty

                ELSE last_valuation_rate
            END AS stock_uom_valuation_rate

        FROM LastSLE
    )


    SELECT
        i.item_group AS item_group,
        qs.item_code AS item_code,
        i.item_name AS item_name,
        i.stock_uom AS stock_uom,

        ws.branch AS branch,
        qs.warehouse AS warehouse,
        qs.quality_status AS quality_status,
        ws.stock_account AS stock_account,

        qs.stock_balance AS stock_balance,

        COALESCE(
            NULLIF(ucd.conversion_factor, 0),
            1
        ) AS ct_conversion_factor,

        qs.stock_balance
        /
        COALESCE(
            NULLIF(ucd.conversion_factor, 0),
            1
        ) AS qty_in_ct,


        /* ---------------------------------------------------------
        Rate Stock UOM -> Rate CT

        Identique pour A, Q, R... du même Item + Warehouse.
        --------------------------------------------------------- */
        wv.stock_uom_valuation_rate
        *
        COALESCE(
            NULLIF(ucd.conversion_factor, 0),
            1
        ) AS valuation_rate,


        /* ---------------------------------------------------------
        Allocation de la valeur warehouse aux Quality Status
        proportionnellement aux quantités.
        --------------------------------------------------------- */
        CASE
            WHEN COALESCE(wv.warehouse_stock_qty, 0) <> 0
            THEN
                wv.warehouse_stock_value
                *
                (
                    qs.stock_balance
                    / wv.warehouse_stock_qty
                )
            ELSE 0
        END AS stock_valuation


    FROM QualityStock qs

    INNER JOIN `tabItem` i
        ON i.item_code = qs.item_code

    INNER JOIN WarehouseScope ws
        ON ws.warehouse = qs.warehouse

    LEFT JOIN WarehouseValuation wv
        ON wv.item_code = qs.item_code
        AND wv.warehouse = qs.warehouse

    LEFT JOIN `tabUOM Conversion Detail` ucd
        ON ucd.parent = qs.item_code
        AND ucd.parenttype = 'Item'
        AND ucd.parentfield = 'uoms'
        AND ucd.uom = 'CT'

    ORDER BY
        ws.branch,
        ws.stock_account,
        qs.item_code,
        qs.warehouse,
        qs.quality_status
    """

    params = [
        company,           # CTE company
        company,           # SLE company
        inventory_date,
        *warehouses,       # BaseSLE warehouse IN (...)
        branch,            # Final branch filter
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

    # For zero theoretical quantity, the snapshot ratio cannot provide a rate.
    # Resolve the latest historical SLE valuation for the Item across the same
    # Company. If none exists, explicitly mark the row for manual valuation.
    zero_qty_items = {
        row.item_code
        for row in rows
        if row.item_code
        and flt(row.qty_in_ct) == 0
        and flt(row.valuation_rate) <= 0
    }
    historical_rates = _get_latest_sle_valuation_rates(
        company=company,
        inventory_date=inventory_date,
        item_codes=zero_qty_items,
    ) if zero_qty_items else {}

    for row in rows:
        row.manual_valuation_required = 0

        if flt(row.qty_in_ct) != 0:
            # Keep the Item + Warehouse rate calculated by the snapshot query.
            continue

        if flt(row.valuation_rate) > 0:
            # A warehouse-level rate already exists. Keep it for this zero-quantity
            # Quality Status instead of replacing it with a Quality-dependent rate.
            continue

        base_rate = flt(historical_rates.get(row.item_code))
        if base_rate > 0:
            row.valuation_rate = flt(
                base_rate * (flt(row.ct_conversion_factor) or 1.0)
            )
            row.manual_valuation_required = 0
        else:
            row.valuation_rate = 0
            row.manual_valuation_required = 1

    return rows


@frappe.whitelist()
def get_manual_inventory_entry_defaults(
    inventory_campaign: str,
    inventory_date: str,
    branch: str,
    item_code: str,
    warehouse: str,
    quality_status: str = "A",
    selected_warehouses=None,
    inventory_count_session: str | None = None,
):
    """Return trusted ERP defaults for a manually discovered inventory line.

    A manual line is used when an Item/Quality Status combination is physically
    found in a selected Warehouse but has no line in the initialized ERP snapshot.
    Quantity and physical value are supplied by the user in the client dialog;
    this method only returns master-data defaults needed to construct the child row.
    """

    if not inventory_campaign:
        frappe.throw("Inventory Campaign is required.")
    if not inventory_date:
        frappe.throw("Inventory Date is required.")
    if not branch:
        frappe.throw("Branch is required.")
    if not item_code:
        frappe.throw("Item is required.")
    if not warehouse:
        frappe.throw("Warehouse is required.")

    selected_warehouses = _normalise_warehouse_list(selected_warehouses)
    if selected_warehouses and warehouse not in selected_warehouses:
        frappe.throw(
            f"Warehouse {warehouse} is not selected in Inventory Count Warehouses."
        )

    _validate_inventory_date_in_campaign_period(
        inventory_campaign=inventory_campaign,
        inventory_date=inventory_date,
    )
    _validate_warehouse_period_exclusivity(
        inventory_campaign=inventory_campaign,
        warehouses=[warehouse],
        current_session=inventory_count_session,
    )

    company = frappe.db.get_value(
        "Inventory Campaign", inventory_campaign, "company"
    )
    if not company:
        frappe.throw(f"Inventory Campaign {inventory_campaign} has no Company.")

    warehouse_data = frappe.db.get_value(
        "Warehouse",
        warehouse,
        ["company", "branch", "account", "parent_warehouse"],
        as_dict=True,
    )
    if not warehouse_data:
        frappe.throw(f"Warehouse {warehouse} does not exist.")
    if warehouse_data.company != company:
        frappe.throw(f"Warehouse {warehouse} does not belong to {company}.")
    if warehouse_data.branch and warehouse_data.branch != branch:
        frappe.throw(f"Warehouse {warehouse} does not belong to Branch {branch}.")

    item = frappe.db.get_value(
        "Item",
        item_code,
        ["item_group", "item_name", "stock_uom", "is_stock_item", "disabled"],
        as_dict=True,
    )
    if not item:
        frappe.throw(f"Item {item_code} does not exist.")
    if cint(item.disabled):
        frappe.throw(f"Item {item_code} is disabled.")
    if not cint(item.is_stock_item):
        frappe.throw(f"Item {item_code} is not a Stock Item.")

    stock_account = warehouse_data.account
    if not stock_account and warehouse_data.parent_warehouse:
        stock_account = frappe.db.get_value(
            "Warehouse", warehouse_data.parent_warehouse, "account"
        )

    ct_conversion_factor = frappe.db.get_value(
        "UOM Conversion Detail",
        {
            "parent": item_code,
            "parenttype": "Item",
            "parentfield": "uoms",
            "uom": "CT",
        },
        "conversion_factor",
    )

    return {
        "item_group": item.item_group,
        "item_code": item_code,
        "item_name": item.item_name,
        "stock_uom": item.stock_uom,
        "warehouse": warehouse,
        "quality_status": quality_status or "A",
        "stock_account": stock_account,
        "ct_conversion_factor": flt(ct_conversion_factor) or 1.0,
    }



def _get_latest_sle_valuation_rates(
    company: str,
    inventory_date: str,
    item_codes,
) -> dict[str, float]:
    """Return the latest positive SLE valuation rate per Item.

    SLE valuation_rate is expressed in the Item stock UOM. The caller converts
    it to CT using each row's CT conversion factor.

    The search is intentionally Company-wide rather than Warehouse-specific:
    the business rule is to use the latest valuation known for the Item in the
    system when the counted warehouse has zero theoretical stock.
    """

    item_codes = sorted({str(x).strip() for x in (item_codes or []) if x})
    if not company or not inventory_date or not item_codes:
        return {}

    placeholders = ", ".join(["%s"] * len(item_codes))
    rows = frappe.db.sql(
        f"""
        SELECT item_code, valuation_rate
        FROM (
            SELECT
                sle.item_code,
                sle.valuation_rate,
                ROW_NUMBER() OVER (
                    PARTITION BY sle.item_code
                    ORDER BY
                        sle.posting_date DESC,
                        sle.posting_time DESC,
                        sle.creation DESC,
                        sle.name DESC
                ) AS row_num
            FROM `tabStock Ledger Entry` sle
            WHERE
                sle.company = %s
                AND sle.posting_date <= %s
                AND sle.item_code IN ({placeholders})
                AND sle.docstatus = 1
                AND sle.is_cancelled = 0
                AND COALESCE(sle.valuation_rate, 0) > 0
        ) latest
        WHERE latest.row_num = 1
        """,
        [company, inventory_date, *item_codes],
        as_dict=True,
    )

    return {
        row.item_code: flt(row.valuation_rate)
        for row in rows
        if flt(row.valuation_rate) > 0
    }



def _get_campaign_period(inventory_campaign: str):
    if not inventory_campaign:
        frappe.throw("Inventory Campaign is required.")

    campaign = frappe.db.get_value(
        "Inventory Campaign",
        inventory_campaign,
        ["start_date", "end_date"],
        as_dict=True,
    )
    if not campaign:
        frappe.throw(f"Inventory Campaign {inventory_campaign} does not exist.")

    if not campaign.start_date or not campaign.end_date:
        frappe.throw(
            f"Inventory Campaign {inventory_campaign} must have Start Date and End Date."
        )

    start_date = getdate(campaign.start_date)
    end_date = getdate(campaign.end_date)

    if start_date > end_date:
        frappe.throw(
            "Inventory Campaign {0} has an invalid period: Start Date {1} is after End Date {2}.".format(
                inventory_campaign,
                start_date,
                end_date,
            )
        )

    return start_date, end_date


def _validate_inventory_date_in_campaign_period(
    inventory_campaign: str,
    inventory_date: str,
):
    if not inventory_date:
        frappe.throw("Inventory Date is required.")

    start_date, end_date = _get_campaign_period(inventory_campaign)
    count_date = getdate(inventory_date)

    if count_date < start_date or count_date > end_date:
        frappe.throw(
            "Inventory Date {0} must be between campaign Start Date {1} and End Date {2}.".format(
                count_date,
                start_date,
                end_date,
            )
        )

    return start_date, end_date


def _get_warehouse_multiselect_config():
    session_meta = frappe.get_meta("Inventory Count Session")
    table_field = session_meta.get_field("inventory_count_warehouses")

    if not table_field or table_field.fieldtype not in ("Table MultiSelect", "Table"):
        frappe.throw(
            "Inventory Count Session field inventory_count_warehouses is missing or is not a table."
        )

    child_doctype = table_field.options
    if not child_doctype:
        frappe.throw(
            "Inventory Count Warehouses must reference a child DocType."
        )

    child_meta = frappe.get_meta(child_doctype)
    warehouse_link = next(
        (
            df
            for df in child_meta.fields
            if df.fieldtype == "Link" and df.options == "Warehouse"
        ),
        None,
    )

    if not warehouse_link:
        frappe.throw(
            f"{child_doctype} must contain a Link field to Warehouse."
        )

    return child_doctype, warehouse_link.fieldname


def _get_selected_session_warehouses(doc) -> list[str]:
    _child_doctype, warehouse_field = _get_warehouse_multiselect_config()

    warehouses = []
    seen = set()

    for row in doc.get("inventory_count_warehouses") or []:
        warehouse = str(row.get(warehouse_field) or "").strip()
        if warehouse and warehouse not in seen:
            seen.add(warehouse)
            warehouses.append(warehouse)

    return warehouses


def _validate_warehouse_period_exclusivity(
    inventory_campaign: str,
    warehouses: list[str],
    current_session: str | None = None,
    lock_warehouses: bool = False,
):
    """Ensure each Warehouse belongs to only one active Count Session in the
    current campaign period.

    The rule is temporal rather than only campaign-name based: any other
    non-cancelled Inventory Count Session whose Inventory Date falls between
    this campaign's Start Date and End Date conflicts with the Warehouse.
    This still allows the same Warehouse to be counted again in a later,
    non-overlapping campaign period.
    """

    warehouses = _normalise_warehouse_list(warehouses)
    if not warehouses:
        return

    start_date, end_date = _get_campaign_period(inventory_campaign)
    child_doctype, warehouse_field = _get_warehouse_multiselect_config()

    # On document Save, lock the selected Warehouse master rows in a stable
    # order. This serialises concurrent attempts to assign the same Warehouse
    # to two Count Sessions. The conflict query below then uses a locking
    # current-read as well, so the second transaction sees the first committed
    # assignment instead of relying on an older repeatable-read snapshot.
    if lock_warehouses:
        warehouses = sorted(warehouses)
        lock_placeholders = ", ".join(["%s"] * len(warehouses))
        frappe.db.sql(
            f"SELECT name FROM `tabWarehouse` WHERE name IN ({lock_placeholders}) ORDER BY name FOR UPDATE",
            warehouses,
        )

    # Metadata-derived identifiers are trusted Frappe schema names. Escape
    # backticks defensively before using them as SQL identifiers.
    child_table = f"tab{child_doctype}".replace("`", "``")
    warehouse_column = warehouse_field.replace("`", "``")
    placeholders = ", ".join(["%s"] * len(warehouses))

    params = [
        current_session or "",
        start_date,
        end_date,
        *warehouses,
    ]

    locking_clause = "FOR UPDATE" if lock_warehouses else ""

    conflicts = frappe.db.sql(
        f"""
        SELECT
            wh.`{warehouse_column}` AS warehouse,
            session.name AS session,
            session.inventory_campaign AS inventory_campaign,
            session.inventory_date AS inventory_date
        FROM `{child_table}` wh
        INNER JOIN `tabInventory Count Session` session
            ON session.name = wh.parent
        INNER JOIN `tabInventory Campaign` campaign
            ON campaign.name = session.inventory_campaign
        WHERE
            wh.parenttype = 'Inventory Count Session'
            AND wh.parentfield = 'inventory_count_warehouses'
            AND session.docstatus < 2
            AND campaign.docstatus < 2
            AND session.name != %s
            AND session.inventory_date BETWEEN %s AND %s
            AND wh.`{warehouse_column}` IN ({placeholders})
        ORDER BY
            wh.`{warehouse_column}`,
            session.inventory_date,
            session.name
        {locking_clause}
        """,
        params,
        as_dict=True,
    )

    if not conflicts:
        return

    lines = []
    for conflict in conflicts:
        lines.append(
            "{0}: already linked to Count Session {1} (Campaign {2}, Inventory Date {3}).".format(
                frappe.bold(conflict.warehouse),
                frappe.bold(conflict.session),
                conflict.inventory_campaign,
                conflict.inventory_date,
            )
        )

    frappe.throw(
        "A Warehouse can belong to only one active Inventory Count Session between {0} and {1}.<br>{2}".format(
            start_date,
            end_date,
            "<br>".join(lines),
        ),
        title="Warehouse already assigned",
    )


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

