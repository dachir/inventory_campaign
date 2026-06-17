# apps/inventory_campaign/inventory_campaign/api/stock_reconciliation.py

"""
Sprint 4 - Controlled import of Inventory Sessions into Stock Reconciliation.

This module implements the ERPNext-side controlled import step:
- only submitted Inventory Sessions can be imported;
- sessions already imported are rejected;
- normal counted items are imported into a draft Stock Reconciliation;
- unplanned_items_json and unplanned_warehouses_json are never imported as stock lines;
- sessions with anomalies or recoding proposals require supervisor review first;
- Stock Reconciliation is saved as Draft and is never submitted automatically.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime


IMPORT_PROTOCOL = "inventory_campaign_stock_reconciliation_import_v1"
REVIEW_STATUSES_ALLOWED_FOR_EXCEPTION_SESSIONS = {"Reviewed", "Approved"}


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None



def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default



def _json_loads(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return frappe.parse_json(value)
        except Exception:
            return json.loads(value)
    return value



def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))



def _as_name_list(value: Any) -> list[str]:
    value = _json_loads(value)

    if value in (None, ""):
        return []

    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]

    if isinstance(value, list):
        names: list[str] = []
        for row in value:
            if isinstance(row, str):
                if row.strip():
                    names.append(row.strip())
            elif isinstance(row, dict):
                name = _safe_str(row.get("name") or row.get("inventory_session") or row.get("session"))
                if name:
                    names.append(name)
        return names

    if isinstance(value, dict):
        for key in ("inventory_sessions", "sessions", "names", "values"):
            inner = value.get(key)
            if inner is not None:
                return _as_name_list(inner)
        name = _safe_str(value.get("name") or value.get("inventory_session") or value.get("session"))
        return [name] if name else []

    return []



def _has_field(doctype: str, fieldname: str) -> bool:
    try:
        return bool(frappe.get_meta(doctype).has_field(fieldname))
    except Exception:
        return False



def _field_value(doc: Any, fieldname: str, default: Any = None) -> Any:
    if not doc:
        return default
    try:
        if _has_field(doc.doctype, fieldname):
            return doc.get(fieldname)
    except Exception:
        pass
    return default


# -----------------------------------------------------------------------------
# Inventory Session validation and import preview
# -----------------------------------------------------------------------------


def _session_requires_supervisor_review(session_doc: Any) -> bool:
    return bool(
        cint(_field_value(session_doc, "has_unplanned_items", 0))
        or cint(_field_value(session_doc, "has_unplanned_warehouses", 0))
        or cint(_field_value(session_doc, "has_recoding_proposals", 0))
    )



def _validate_session_for_import(session_doc: Any, strict_review: bool = True) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if not session_doc:
        return ["Inventory Session not found."], warnings

    if session_doc.status != "Submitted":
        errors.append(f"Session {session_doc.name} must have status Submitted, current status is {session_doc.status}.")

    if _safe_str(_field_value(session_doc, "imported_stock_reconciliation")):
        errors.append(
            f"Session {session_doc.name} was already imported into Stock Reconciliation "
            f"{_field_value(session_doc, 'imported_stock_reconciliation')}.")

    if not session_doc.get("items"):
        errors.append(f"Session {session_doc.name} has no counted items to import.")

    if _session_requires_supervisor_review(session_doc):
        review_status = _safe_str(_field_value(session_doc, "review_status")) or "Pending"
        if strict_review and review_status not in REVIEW_STATUSES_ALLOWED_FOR_EXCEPTION_SESSIONS:
            errors.append(
                f"Session {session_doc.name} has unplanned discoveries or recoding proposals. "
                f"It must be Reviewed or Approved before Stock Reconciliation import. "
                f"Current review_status is {review_status}."
            )
        else:
            warnings.append(
                f"Session {session_doc.name} has unplanned discoveries or recoding proposals. "
                "Only normal counted item rows will be imported. JSON evidence remains on the session."
            )

    return errors, warnings



def _session_filters(campaign: str | None = None, warehouse: str | None = None, inventory_agent: str | None = None) -> dict[str, Any]:
    filters: dict[str, Any] = {"status": "Submitted"}
    if campaign:
        filters["campaign"] = campaign
    if warehouse:
        # Inventory Session.warehouse is the parent campaign warehouse in this model.
        filters["warehouse"] = warehouse
    if inventory_agent:
        filters["inventory_agent"] = inventory_agent
    return filters



def _load_sessions_or_throw(session_names: list[str]) -> list[Any]:
    docs = []
    for name in session_names:
        if not frappe.db.exists("Inventory Session", name):
            frappe.throw(f"Inventory Session does not exist: {name}")
        docs.append(frappe.get_doc("Inventory Session", name))
    return docs



def _counted_line_warehouse(session_doc: Any, item_row: Any) -> str | None:
    return (
        _safe_str(item_row.get("location_warehouse"))
        or _safe_str(session_doc.get("location_warehouse"))
        or _safe_str(session_doc.get("parent_warehouse"))
        or _safe_str(session_doc.get("warehouse"))
    )



def _get_valuation_rate(item_code: str, warehouse: str | None = None) -> float:
    valuation_rate = None

    if item_code and warehouse:
        try:
            valuation_rate = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "valuation_rate")
        except Exception:
            valuation_rate = None

    if not valuation_rate:
        try:
            valuation_rate = frappe.db.get_value("Item", item_code, "valuation_rate")
        except Exception:
            valuation_rate = None

    if not valuation_rate:
        try:
            valuation_rate = frappe.db.get_value("Item", item_code, "last_purchase_rate")
        except Exception:
            valuation_rate = None

    return flt(valuation_rate)



def _aggregate_session_items(session_docs: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    aggregate: dict[tuple[str, str], dict[str, Any]] = {}

    for session_doc in session_docs:
        for row in session_doc.get("items") or []:
            item_code = _safe_str(row.get("item_code"))
            warehouse = _counted_line_warehouse(session_doc, row)
            if not item_code:
                errors.append({"session": session_doc.name, "reason": "Counted item row is missing item_code."})
                continue
            if not warehouse:
                errors.append({"session": session_doc.name, "item_code": item_code, "reason": "Counted item row has no warehouse/location."})
                continue

            counted_qty = flt(row.get("counted_qty"))
            key = (item_code, warehouse)
            if key not in aggregate:
                aggregate[key] = {
                    "item_code": item_code,
                    "item_name": _safe_str(row.get("item_name")) or frappe.db.get_value("Item", item_code, "item_name"),
                    "warehouse": warehouse,
                    "qty": 0.0,
                    "uom": _safe_str(row.get("uom")) or frappe.db.get_value("Item", item_code, "stock_uom"),
                    "valuation_rate": _get_valuation_rate(item_code, warehouse),
                    "source_sessions": [],
                    "source_lines": 0,
                }

            aggregate[key]["qty"] = flt(aggregate[key]["qty"]) + counted_qty
            aggregate[key]["source_lines"] = _safe_int(aggregate[key].get("source_lines"), 0) + 1
            if session_doc.name not in aggregate[key]["source_sessions"]:
                aggregate[key]["source_sessions"].append(session_doc.name)

    lines = sorted(aggregate.values(), key=lambda row: (row.get("warehouse") or "", row.get("item_code") or ""))
    return lines, errors



def _get_single_value(values: set[str | None], label: str) -> str | None:
    clean = {value for value in values if value}
    if not clean:
        return None
    if len(clean) > 1:
        frappe.throw(f"Selected Inventory Sessions must have one {label}. Found: {', '.join(sorted(clean))}")
    return next(iter(clean))


# -----------------------------------------------------------------------------
# Public APIs
# -----------------------------------------------------------------------------


@frappe.whitelist()
def get_importable_inventory_sessions(
    campaign: str | None = None,
    warehouse: str | None = None,
    inventory_agent: str | None = None,
    strict_review: int = 1,
    limit_page_length: int = 50,
) -> dict[str, Any]:
    """Return submitted Inventory Sessions that can be considered for import."""

    campaign = _safe_str(campaign)
    warehouse = _safe_str(warehouse)
    inventory_agent = _safe_str(inventory_agent)
    strict_review_bool = bool(cint(strict_review))

    rows = frappe.get_all(
        "Inventory Session",
        filters=_session_filters(campaign=campaign, warehouse=warehouse, inventory_agent=inventory_agent),
        fields=[
            "name",
            "campaign",
            "warehouse",
            "parent_warehouse",
            "location_warehouse",
            "inventory_agent",
            "operator_name",
            "status",
            "review_status",
            "total_items_counted",
            "total_qty_counted",
            "has_unplanned_items",
            "unplanned_items_count",
            "has_unplanned_warehouses",
            "unplanned_warehouses_count",
            "has_recoding_proposals",
            "recoding_proposals_count",
            "imported_stock_reconciliation",
            "submitted_at",
            "modified",
        ],
        order_by="submitted_at desc, modified desc",
        limit_page_length=_safe_int(limit_page_length, 50),
    )

    result_rows: list[dict[str, Any]] = []
    for row in rows:
        if _safe_str(row.get("imported_stock_reconciliation")):
            continue

        session_doc = frappe.get_doc("Inventory Session", row.name)
        errors, warnings = _validate_session_for_import(session_doc, strict_review=strict_review_bool)
        out = dict(row)
        out["requires_supervisor_review"] = _session_requires_supervisor_review(session_doc)
        out["importable"] = not errors
        out["errors"] = errors
        out["warnings"] = warnings
        result_rows.append(out)

    return {
        "ok": True,
        "sessions": result_rows,
        "count": len(result_rows),
        "strict_review": strict_review_bool,
    }


@frappe.whitelist()
def preview_inventory_session_import(
    inventory_sessions: Any,
    strict_review: int = 1,
) -> dict[str, Any]:
    """Validate and preview the Stock Reconciliation lines for selected sessions."""

    session_names = _as_name_list(inventory_sessions)
    if not session_names:
        return {"ok": False, "reason": "At least one Inventory Session is required."}

    session_docs = _load_sessions_or_throw(session_names)
    strict_review_bool = bool(cint(strict_review))

    errors: list[str] = []
    warnings: list[str] = []
    for session_doc in session_docs:
        session_errors, session_warnings = _validate_session_for_import(session_doc, strict_review=strict_review_bool)
        errors.extend(session_errors)
        warnings.extend(session_warnings)

    companies = {session_doc.get("company") for session_doc in session_docs}
    campaigns = {session_doc.get("campaign") for session_doc in session_docs}
    warehouses = {session_doc.get("warehouse") or session_doc.get("parent_warehouse") for session_doc in session_docs}

    company = None
    campaign = None
    warehouse = None
    try:
        company = _get_single_value(companies, "company")
        campaign = _get_single_value(campaigns, "campaign")
        warehouse = _get_single_value(warehouses, "warehouse")
    except Exception as exc:
        errors.append(str(exc))

    lines, line_errors = _aggregate_session_items(session_docs)
    for row in line_errors:
        errors.append(row.get("reason") or str(row))

    if not lines:
        errors.append("Selected Inventory Sessions do not produce any Stock Reconciliation line.")

    return {
        "ok": not errors,
        "company": company,
        "campaign": campaign,
        "warehouse": warehouse,
        "sessions": session_names,
        "session_count": len(session_names),
        "line_count": len(lines),
        "lines": lines,
        "errors": errors,
        "warnings": warnings,
        "strict_review": strict_review_bool,
    }


@frappe.whitelist()
def import_inventory_sessions(
    stock_reconciliation: str,
    inventory_sessions: Any,
    strict_review: int = 1,
    merge_existing_rows: int = 1,
) -> dict[str, Any]:
    """
    Import selected Inventory Sessions into a draft Stock Reconciliation.

    The Stock Reconciliation is saved but not submitted. Imported Inventory
    Sessions are marked as Imported to block double import.
    """

    stock_reconciliation = _safe_str(stock_reconciliation)
    if not stock_reconciliation:
        return {"ok": False, "reason": "stock_reconciliation is required."}

    if not frappe.db.exists("Stock Reconciliation", stock_reconciliation):
        return {"ok": False, "reason": f"Stock Reconciliation does not exist: {stock_reconciliation}"}

    sr_doc = frappe.get_doc("Stock Reconciliation", stock_reconciliation)
    if sr_doc.docstatus != 0:
        return {"ok": False, "reason": "Inventory Sessions can only be imported into a draft Stock Reconciliation."}

    preview = preview_inventory_session_import(inventory_sessions=inventory_sessions, strict_review=strict_review)
    if not preview.get("ok"):
        return preview

    if preview.get("company") and sr_doc.company and preview.get("company") != sr_doc.company:
        return {
            "ok": False,
            "reason": "Stock Reconciliation company does not match selected Inventory Sessions.",
            "stock_reconciliation_company": sr_doc.company,
            "inventory_session_company": preview.get("company"),
        }

    if _has_field("Stock Reconciliation", "custom_inventory_campaign"):
        existing_campaign = _safe_str(sr_doc.get("custom_inventory_campaign"))
        if existing_campaign and existing_campaign != preview.get("campaign"):
            return {
                "ok": False,
                "reason": "Stock Reconciliation already references another Inventory Campaign.",
                "stock_reconciliation_campaign": existing_campaign,
                "inventory_session_campaign": preview.get("campaign"),
            }

    existing_rows_by_key: dict[tuple[str, str], Any] = {}
    if bool(cint(merge_existing_rows)):
        for row in sr_doc.get("items") or []:
            item_code = _safe_str(row.get("item_code"))
            warehouse = _safe_str(row.get("warehouse"))
            if item_code and warehouse:
                existing_rows_by_key[(item_code, warehouse)] = row

    imported_line_count = 0
    for line in preview.get("lines") or []:
        item_code = _safe_str(line.get("item_code"))
        warehouse = _safe_str(line.get("warehouse"))
        qty = flt(line.get("qty"))
        if not item_code or not warehouse:
            continue

        existing_row = existing_rows_by_key.get((item_code, warehouse))
        if existing_row:
            existing_row.qty = flt(existing_row.get("qty")) + qty
            if not flt(existing_row.get("valuation_rate")) and flt(line.get("valuation_rate")):
                existing_row.valuation_rate = flt(line.get("valuation_rate"))
        else:
            row_data = {
                "item_code": item_code,
                "warehouse": warehouse,
                "qty": qty,
            }
            if line.get("valuation_rate") is not None:
                row_data["valuation_rate"] = flt(line.get("valuation_rate"))
            sr_doc.append("items", row_data)
            existing_rows_by_key[(item_code, warehouse)] = sr_doc.get("items")[-1]
        imported_line_count += 1

    if _has_field("Stock Reconciliation", "custom_inventory_source"):
        sr_doc.custom_inventory_source = "Inventory Campaign"
    if _has_field("Stock Reconciliation", "custom_inventory_campaign"):
        sr_doc.custom_inventory_campaign = preview.get("campaign")
    if _has_field("Stock Reconciliation", "custom_inventory_session_refs"):
        sr_doc.custom_inventory_session_refs = _build_session_refs(sr_doc, preview)

    try:
        sr_doc.save(ignore_permissions=True)

        imported_at = now_datetime()
        session_values = {
            "status": "Imported",
            "review_status": "Imported",
            "imported_stock_reconciliation": sr_doc.name,
            "imported_at": imported_at,
        }
        if _has_field("Inventory Session", "imported_by"):
            session_values["imported_by"] = frappe.session.user

        for session_name in preview.get("sessions") or []:
            frappe.db.set_value("Inventory Session", session_name, session_values, update_modified=True)

        frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(
            title="Inventory Campaign - import_inventory_sessions_failed",
            message=frappe.get_traceback(),
        )
        return {
            "ok": False,
            "reason": "Inventory Sessions could not be imported into Stock Reconciliation.",
            "exception": str(exc),
        }

    return {
        "ok": True,
        "stock_reconciliation": sr_doc.name,
        "campaign": preview.get("campaign"),
        "sessions": preview.get("sessions"),
        "session_count": preview.get("session_count"),
        "imported_line_count": imported_line_count,
        "stock_reconciliation_line_count": len(sr_doc.get("items") or []),
        "warnings": preview.get("warnings") or [],
        "submitted": False,
        "message": "Inventory Sessions imported into draft Stock Reconciliation. Review and submit manually.",
    }


# -----------------------------------------------------------------------------
# Stock Reconciliation trace helpers
# -----------------------------------------------------------------------------


def _build_session_refs(sr_doc: Any, preview: dict[str, Any]) -> str:
    current = _json_loads(sr_doc.get("custom_inventory_session_refs")) if sr_doc.get("custom_inventory_session_refs") else None
    if not isinstance(current, list):
        current = []

    current.append({
        "protocol": IMPORT_PROTOCOL,
        "imported_at": str(now_datetime()),
        "imported_by": frappe.session.user,
        "campaign": preview.get("campaign"),
        "sessions": preview.get("sessions") or [],
        "session_count": preview.get("session_count"),
        "line_count": preview.get("line_count"),
        "lines": [
            {
                "item_code": row.get("item_code"),
                "warehouse": row.get("warehouse"),
                "qty": row.get("qty"),
                "source_sessions": row.get("source_sessions"),
            }
            for row in preview.get("lines") or []
        ],
        "note": "Stock Reconciliation remains draft; submission is manual.",
    })

    return _json_dumps(current)
