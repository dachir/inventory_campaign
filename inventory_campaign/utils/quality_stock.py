# inventory_campaign/utils/quality_stock.py

"""Quality-status stock snapshot helpers for Inventory Campaign.

The Inventory Campaign flow must not rely on Bin.actual_qty when the stock
has to be audited by ``quality_status``. Bin only stores the total by
item/warehouse. The authoritative split by quality status is reconstructed
from Stock Ledger Entry.

Important integration rules:
- The Quality Status master data belongs to the separate ``Quality Control`` app.
- This app does not create or seed the ``Quality Status`` DocType.
- If the DocType exists, its records are used to keep a stable status order.
- If it is not installed yet, the code falls back to A/Q/R/5K for development.
- The saved JSON is intentionally compact: only positive warehouse/status balances.
- The snapshot for a counted item is scoped to the session warehouse tree:
  all descendant warehouses with positive stock are returned, grouped by
  warehouse and quality_status.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import flt, get_datetime, now_datetime


QUALITY_STATUS_DOCTYPE = "Quality Status"
FALLBACK_QUALITY_STATUSES = ("A", "Q", "R", "5K")
DEFAULT_QUALITY_STATUS = "A"
_LOCAL_CACHE_ATTR = "inventory_campaign_quality_status_names"


def json_dumps(data: Any) -> str:
    """Serialize JSON in a stable and compact format for DocType fields."""

    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def has_field(doctype: str, fieldname: str) -> bool:
    try:
        return bool(frappe.get_meta(doctype).has_field(fieldname))
    except Exception:
        return False


def doctype_exists(doctype: str) -> bool:
    """Return True if a DocType/table exists without forcing a dependency."""

    try:
        if frappe.db.exists("DocType", doctype):
            return True
    except Exception:
        pass

    try:
        return bool(frappe.db.table_exists(doctype))
    except Exception:
        return False


def _deduplicate(values: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        status = str(value or "").strip()
        if status and status not in seen:
            seen.add(status)
            result.append(status)
    return result


def _get_local_cache() -> tuple[str, ...] | None:
    try:
        return getattr(frappe.local, _LOCAL_CACHE_ATTR, None)
    except Exception:
        return None


def _set_local_cache(statuses: list[str] | tuple[str, ...]) -> None:
    try:
        setattr(frappe.local, _LOCAL_CACHE_ATTR, tuple(statuses))
    except Exception:
        pass


def get_quality_status_names(force_reload: bool = False) -> list[str]:
    """Return the active Quality Status names used by Stock Ledger Entry.

    The canonical source is the ``Quality Status`` DocType provided by the
    Quality Control app. A small per-request cache avoids one master-data query
    per counted line.

    Fallback exists only to keep development/test environments working when the
    Quality Control app is not installed yet.
    """

    if not force_reload:
        cached = _get_local_cache()
        if cached:
            return list(cached)

    statuses: list[str] = []

    if doctype_exists(QUALITY_STATUS_DOCTYPE):
        filters: dict[str, Any] = {}
        try:
            meta = frappe.get_meta(QUALITY_STATUS_DOCTYPE)
            if meta.has_field("disabled"):
                filters["disabled"] = 0
        except Exception:
            pass

        try:
            statuses = frappe.get_all(
                QUALITY_STATUS_DOCTYPE,
                filters=filters,
                pluck="name",
                order_by="idx asc, name asc",
            )
        except TypeError:
            # Compatibility fallback if pluck is not available.
            rows = frappe.get_all(
                QUALITY_STATUS_DOCTYPE,
                filters=filters,
                fields=["name"],
                order_by="idx asc, name asc",
            )
            statuses = [row.get("name") for row in rows]

    statuses = _deduplicate(statuses)

    if not statuses:
        statuses = list(FALLBACK_QUALITY_STATUSES)

    # Legacy SLE rows with blank quality_status are treated as A. Keep A visible
    # even if a test database has not created the master record yet.
    if DEFAULT_QUALITY_STATUS not in statuses:
        statuses.insert(0, DEFAULT_QUALITY_STATUS)

    _set_local_cache(statuses)
    return statuses


def _posting_cutoff(snapshot_datetime: Any | None = None) -> tuple[Any, str]:
    snapshot_dt = get_datetime(snapshot_datetime or now_datetime())
    return snapshot_dt.date(), snapshot_dt.strftime("%H:%M:%S")


def _status_sort_key(status: str, configured_statuses: list[str]) -> tuple[int, str]:
    try:
        return configured_statuses.index(status), status
    except ValueError:
        return len(configured_statuses), status


def _get_warehouse_tree_bounds(warehouse: str) -> dict[str, Any]:
    """Return NestedSet bounds for a Warehouse or fail explicitly.

    ERPNext Warehouse is a tree. The inventory stock snapshot must be limited to
    the session warehouse and its descendants, never to the entire company.
    """

    warehouse = (warehouse or "").strip()
    if not warehouse:
        frappe.throw("warehouse_scope is required to calculate stock inside the session warehouse tree.")

    row = frappe.db.get_value("Warehouse", warehouse, ["name", "lft", "rgt"], as_dict=True)
    if not row:
        frappe.throw(f"Warehouse {warehouse} does not exist. Cannot calculate scoped system stock.")

    if row.get("lft") is None or row.get("rgt") is None:
        frappe.throw(f"Warehouse {warehouse} has no tree bounds. Cannot calculate descendant stock safely.")

    return row


def is_warehouse_descendant_or_self(warehouse: str | None, root_warehouse: str | None) -> bool:
    """Return whether ``warehouse`` is inside ``root_warehouse`` tree."""

    warehouse = (warehouse or "").strip()
    root_warehouse = (root_warehouse or "").strip()
    if not warehouse or not root_warehouse:
        return False

    try:
        root = _get_warehouse_tree_bounds(root_warehouse)
        row = frappe.db.get_value("Warehouse", warehouse, ["lft", "rgt"], as_dict=True)
        if not row or row.get("lft") is None or row.get("rgt") is None:
            return False
        return row.get("lft") >= root.get("lft") and row.get("rgt") <= root.get("rgt")
    except Exception:
        return False


def get_quality_status_stock_snapshot(
    item_code: str,
    warehouse: str | None = None,
    snapshot_datetime: Any | None = None,
    warehouse_scope: str | None = None,
) -> list[dict[str, Any]]:
    """Return a compact stock snapshot for one Item.

    Saved JSON format on Inventory Session Item::

        [
            {"warehouse": "04AD03 - MCO", "status": "A", "qty": 10.0},
            {"warehouse": "04AD03 - MCO", "status": "R", "qty": 3.0},
            {"warehouse": "05AA01 - MCO", "status": "5K", "qty": 2.0}
        ]

    Rules:
    - source is Stock Ledger Entry, not Bin;
    - only submitted, non-cancelled SLE rows are considered;
    - empty legacy quality_status values are treated as A;
    - status order follows the Quality Status DocType when available;
    - only warehouse/status combinations with qty > 0 are stored;
    - if ``warehouse_scope`` is passed, the result is limited to that Warehouse
      and all its descendants in the ERPNext Warehouse tree;
    - if ``warehouse`` is passed without a scope, the result is limited to that
      exact warehouse;
    - if both are empty, the result covers all warehouses for the item. This is
      kept only for diagnostic/backward-compatible usage, not for the mobile
      session submission flow.

    The Inventory Session API calls this with ``warehouse_scope`` equal to the
    session/campaign warehouse, so the line snapshot shows where the item exists
    inside the authorised inventory perimeter only.
    """

    item_code = (item_code or "").strip()
    warehouse = (warehouse or "").strip()

    if not item_code:
        frappe.throw("item_code is required to calculate quality-status stock.")

    if not has_field("Stock Ledger Entry", "quality_status"):
        frappe.throw("Stock Ledger Entry.quality_status field is missing.")

    configured_statuses = get_quality_status_names()
    snapshot_dt = get_datetime(snapshot_datetime or now_datetime())
    posting_date, posting_time = _posting_cutoff(snapshot_dt)

    warehouse_join = ""
    warehouse_condition = ""
    params: dict[str, Any] = {
        "item_code": item_code,
        "posting_date": posting_date,
        "posting_time": posting_time,
        "default_quality_status": DEFAULT_QUALITY_STATUS,
    }

    warehouse_scope = (warehouse_scope or "").strip()
    if warehouse_scope:
        scope = _get_warehouse_tree_bounds(warehouse_scope)
        warehouse_join = "INNER JOIN `tabWarehouse` w ON w.name = s.warehouse"
        warehouse_condition = "AND w.lft >= %(scope_lft)s AND w.rgt <= %(scope_rgt)s"
        params["scope_lft"] = scope.get("lft")
        params["scope_rgt"] = scope.get("rgt")
    elif warehouse:
        warehouse_condition = "AND s.warehouse = %(warehouse)s"
        params["warehouse"] = warehouse

    rows = frappe.db.sql(
        f"""
        SELECT
            s.warehouse AS warehouse,
            COALESCE(NULLIF(s.quality_status, ''), %(default_quality_status)s) AS quality_status,
            SUM(s.actual_qty) AS actual_qty
        FROM
            `tabStock Ledger Entry` s
            {warehouse_join}
        WHERE
            s.item_code = %(item_code)s
            {warehouse_condition}
            AND s.docstatus = 1
            AND s.is_cancelled = 0
            AND (
                s.posting_date < %(posting_date)s
                OR (
                    s.posting_date = %(posting_date)s
                    AND IFNULL(s.posting_time, '00:00:00') <= %(posting_time)s
                )
            )
        GROUP BY
            s.warehouse,
            COALESCE(NULLIF(s.quality_status, ''), %(default_quality_status)s)
        HAVING
            SUM(s.actual_qty) > 0
        """,
        params,
        as_dict=True,
    )

    compact_rows: list[dict[str, Any]] = []
    for row in rows:
        row_warehouse = str(row.get("warehouse") or "").strip()
        status = str(row.get("quality_status") or DEFAULT_QUALITY_STATUS).strip() or DEFAULT_QUALITY_STATUS
        qty = flt(row.get("actual_qty") or 0)

        if row_warehouse and qty > 0:
            compact_rows.append({"warehouse": row_warehouse, "status": status, "qty": qty})

    compact_rows.sort(key=lambda row: (row.get("warehouse") or "", _status_sort_key(row.get("status") or "", configured_statuses)))
    return compact_rows


def get_counted_qty_from_row(row: Any) -> float:
    """Read counted quantity from the current Inventory Session Item shape."""

    total_counted_qty = getattr(row, "total_counted_qty", None)
    if total_counted_qty not in (None, ""):
        return flt(total_counted_qty)

    qty_usable = flt(getattr(row, "qty_usable", 0) or 0)
    qty_damaged = flt(getattr(row, "qty_damaged", 0) or 0)
    qty_to_verify = flt(getattr(row, "qty_to_verify", 0) or 0)
    apparent_state_total = qty_usable + qty_damaged + qty_to_verify

    if apparent_state_total:
        return flt(apparent_state_total)

    return flt(getattr(row, "counted_qty", 0) or 0)


def get_system_qty_from_snapshot(snapshot: Any, quality_status: str | None = None) -> float:
    """Return total stock, or the stock of one quality_status if provided.

    The current JSON shape is a compact list of positive warehouse/status rows.
    A legacy dict reader is kept so older saved rows or partially updated sites do
    not break immediately.
    """

    status = (quality_status or "").strip()

    if isinstance(snapshot, list):
        total = 0.0
        for row in snapshot:
            if not isinstance(row, dict):
                continue
            row_status = str(row.get("status") or "").strip()
            if status and row_status != status:
                continue
            total += flt(row.get("qty") or 0)
        return flt(total)

    if isinstance(snapshot, dict):
        if not status:
            return flt(snapshot.get("total_qty") or 0)

        by_status = snapshot.get("by_quality_status") or {}
        extra_statuses = snapshot.get("extra_quality_statuses") or {}

        if status in by_status:
            return flt(by_status.get(status) or 0)

        if status in extra_statuses:
            return flt(extra_statuses.get(status) or 0)

    return 0.0


def apply_stock_snapshot_to_session(doc: Any, force: bool = False) -> None:
    """Fill compact system stock JSON on each Inventory Session Item row.

    By default, an existing ``system_stock_json`` is not overwritten. This keeps
    the session as an immutable snapshot of the ERP stock at the time the mobile
    submission was received.

    The snapshot is calculated for the item across all descendant warehouses of
    the session warehouse. The row's ``location_warehouse`` remains the physical
    counted location; it is not used as the stock filter anymore.
    """

    if not doc.get("items"):
        return

    snapshot_dt = (
        getattr(doc, "server_ack_at", None)
        or getattr(doc, "submitted_at", None)
        or now_datetime()
    )

    child_has_json = has_field("Inventory Session Item", "system_stock_json")
    child_has_system_qty = has_field("Inventory Session Item", "system_qty")
    child_has_difference_qty = has_field("Inventory Session Item", "difference_qty")
    child_has_quality_status = has_field("Inventory Session Item", "quality_status")
    warehouse_scope = (
        getattr(doc, "parent_warehouse", None)
        or getattr(doc, "warehouse", None)
    )

    for row in doc.get("items") or []:
        item_code = getattr(row, "item_code", None)
        if not item_code:
            continue

        if not force and child_has_json and getattr(row, "system_stock_json", None):
            continue

        snapshot = get_quality_status_stock_snapshot(
            item_code=item_code,
            snapshot_datetime=snapshot_dt,
            warehouse_scope=warehouse_scope,
        )

        row_quality_status = getattr(row, "quality_status", None) if child_has_quality_status else None
        system_qty = get_system_qty_from_snapshot(snapshot, row_quality_status)

        if child_has_json:
            row.system_stock_json = json_dumps(snapshot)

        if child_has_system_qty:
            row.system_qty = system_qty

        if child_has_difference_qty:
            row.difference_qty = flt(get_counted_qty_from_row(row)) - flt(system_qty)
