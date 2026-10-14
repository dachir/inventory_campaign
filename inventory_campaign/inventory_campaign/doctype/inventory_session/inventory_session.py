# Copyright (c) 2026, Richard Amouzou and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import flt, getdate, nowdate


class InventorySession(Document):
    pass


@frappe.whitelist()
def get_inventory_session_audit(inventory_session: str) -> dict:
    """Preview the shared reconciliation, including zero-variance audit report."""
    plan = _build_inventory_audit_plan(inventory_session)
    return {"summary": plan["summary"], "report": plan["report"]}


@frappe.whitelist()
def create_inventory_count_session(inventory_session: str, mode: str = "variance") -> dict:
    """Generate only variance or zero-variance relocation lines; Draft insert only."""
    if mode not in ("variance", "zero_relocation"):
        frappe.throw("Unknown transfer mode.")
    plan = _build_inventory_audit_plan(inventory_session)
    source = plan["source"]
    # One active result per source and mode, without introducing a new DocType field.
    existing_names = frappe.get_all(
        "Inventory Count Session",
        filters={"source_inventory_session": source.name, "docstatus": ["<", 2]},
        pluck="name",
    )
    for name in existing_names:
        first_line = frappe.db.get_value(
            "Inventory Count Session Detail",
            {"parent": name, "parenttype": "Inventory Count Session",
             "parentfield": "table_uawl"}, "source_inventory_json",
            order_by="idx asc",
        )
        try:
            info = frappe.parse_json(first_line or "{}")
        except (ValueError, TypeError):
            info = {}
        prior_mode = info.get("transfer_mode", "variance")
        if prior_mode == mode:
            frappe.throw("An active Inventory Count Session {0} already exists for this transfer type.".format(name))

    selected = []
    for entry in plan["records"]:
        item = entry["item_code"]
        delta = entry["difference_qty_stock_uom"]
        if mode == "variance":
            include = item not in plan["zero_items"] and abs(delta) > 0.000001
        else:
            # Zero total variance is insufficient: actual local warehouse moves required.
            include = item in plan["moved_items"] and abs(delta) > 0.000001
        if include:
            selected.append(entry)

    if not selected:
        return {"created": False, "name": None, "mode": mode,
                "message": ("No location changes to validate." if mode == "zero_relocation"
                            else "No non-zero inventory differences require treatment."),
                "summary": plan["summary"]}

    count_session = frappe.new_doc("Inventory Count Session")
    count_session.inventory_campaign = source.campaign
    count_session.branch = plan["campaign"].branch
    count_session.inventory_date = plan["target_date"]
    count_session.source_inventory_session = source.name
    involved_warehouses = sorted({e["warehouse"] for e in selected})
    for warehouse in involved_warehouses:
        count_session.append("inventory_count_warehouses", {"warehouse": warehouse})
    for entry in selected:
        values = dict(entry["detail_values"])
        evidence = frappe.parse_json(values["source_inventory_json"])
        evidence["transfer_mode"] = mode
        values["source_inventory_json"] = frappe.as_json(evidence)
        count_session.append("table_uawl", values)
    count_session.insert()
    return {"created": True, "name": count_session.name, "mode": mode,
            "summary": plan["summary"], "lines": len(selected)}


def _build_inventory_audit_plan(inventory_session: str) -> dict:
    """Single source of truth: clean, match, roll-forward and classify."""
    if not inventory_session:
        frappe.throw("Inventory Session is required.")

    source = frappe.get_doc("Inventory Session", inventory_session)
    source.check_permission("read")

    if source.docstatus != 1:
        frappe.throw(
            "Inventory Session must be submitted before creating an Inventory Count Session."
        )

    if not source.campaign:
        frappe.throw("Inventory Session has no Inventory Campaign.")

    campaign = frappe.db.get_value(
        "Inventory Campaign",
        source.campaign,
        ["company", "branch", "start_date", "end_date"],
        as_dict=True,
    )
    if not campaign:
        frappe.throw("Inventory Campaign {0} does not exist.".format(source.campaign))
    if not campaign.branch:
        frappe.throw("Inventory Campaign {0} has no Branch.".format(source.campaign))

    historical_inventory_date = getdate(
        source.server_ack_at
        or source.submitted_at
        or source.closed_at
        or source.creation
    )
    target_inventory_date = getdate(nowdate())

    # ------------------------------------------------------------------
    # 1. Clean historical mobile data in memory.
    # ------------------------------------------------------------------
    physical_rows, system_pool = _prepare_inventory_session_rows(source)
    _match_physical_rows_to_system(physical_rows, system_pool)
    clean_rows = _build_clean_physical_rows(source.name, physical_rows)

    item_codes = sorted({row["item_code"] for row in physical_rows if row.get("item_code")})
    if not item_codes:
        frappe.throw("Inventory Session contains no countable Item rows.")

    warehouses = _collect_target_warehouses(physical_rows, system_pool)
    if not warehouses:
        frappe.throw(
            "No Warehouse could be resolved from Inventory Session lines or system_stock_json."
        )

    # ------------------------------------------------------------------
    # 2. Build historical quantities from the evidence stored in the
    #    Inventory Session itself.  No old Stock Ledger reconstruction is
    #    required: system_stock_json is the historical system snapshot.
    # ------------------------------------------------------------------
    historical_physical = _build_historical_physical_by_key(clean_rows)
    historical_system = _build_historical_system_by_key(system_pool)

    # ------------------------------------------------------------------
    # 3. Read today's ERP stock through the exact same snapshot function used
    #    by Initialize Inventory / Recalculate Values.
    # ------------------------------------------------------------------
    from inventory_campaign.inventory_campaign.doctype.inventory_count_session.inventory_count_session import (
        _query_inventory_snapshot,
    )

    current_snapshot_rows = _query_inventory_snapshot(
        company=campaign.company,
        inventory_date=str(target_inventory_date),
        branch=campaign.branch,
        warehouses=warehouses,
    )

    current_snapshot_rows = [
        row for row in current_snapshot_rows
        if row.get("item_code") in item_codes
    ]

    current_by_key = {
        (
            row.get("item_code"),
            row.get("warehouse"),
            row.get("quality_status") or "A",
        ): row
        for row in current_snapshot_rows
    }

    # Include every historical physical/system key plus every current system key
    # for the source Items inside the Inventory Session warehouse scope.
    all_keys = set(historical_physical) | set(historical_system) | set(current_by_key)

    if not all_keys:
        frappe.throw("No Inventory Count Session detail could be generated from this Inventory Session.")

    # ------------------------------------------------------------------
    # 4. Create the Draft Count Session for TODAY and roll the historical
    #    variance forward onto today's theoretical stock.
    # ------------------------------------------------------------------
    defaults_cache = {}
    detail_records = []
    negative_rows = 0

    for key in sorted(all_keys):
        item_code, warehouse, quality_status = key

        historical_physical_entry = historical_physical.get(key) or {}
        historical_system_entry = historical_system.get(key) or {}
        current = current_by_key.get(key)

        historical_physical_qty = flt(historical_physical_entry.get("qty"))
        historical_system_qty = flt(historical_system_entry.get("qty"))
        historical_difference_qty = flt(
            historical_physical_qty - historical_system_qty
        )

        current_system_qty = flt(current.get("stock_balance")) if current else 0.0
        adjusted_physical_qty = flt(
            current_system_qty + historical_difference_qty
        )

        # Avoid tiny negative values caused only by floating-point arithmetic.
        if abs(adjusted_physical_qty) <= 0.000001:
            adjusted_physical_qty = 0.0

        negative_stock = 1 if adjusted_physical_qty < 0 else 0
        if negative_stock:
            negative_rows += 1

        if current:
            factor = flt(current.get("ct_conversion_factor")) or 1.0
            detail_values = {
                "item_group": current.get("item_group"),
                "item_code": current.get("item_code"),
                "item_name": current.get("item_name"),
                "stock_uom": current.get("stock_uom"),
                "warehouse": current.get("warehouse"),
                "quality_status": current.get("quality_status") or "A",
                "stock_account": current.get("stock_account"),
                "stock_balance": flt(current.get("stock_balance")),
                "ct_conversion_factor": factor,
                "qty_in_ct": flt(current.get("qty_in_ct")),
                "valuation_rate": flt(current.get("valuation_rate")),
                "stock_valuation": flt(current.get("stock_valuation")),
                "manual_valuation_required": int(
                    current.get("manual_valuation_required") or 0
                ),
            }
        else:
            defaults_key = (item_code, warehouse)
            if defaults_key not in defaults_cache:
                defaults_cache[defaults_key] = _get_mobile_detail_defaults(
                    item_code=item_code, warehouse=warehouse,
                    quality_status=quality_status,
                )

            defaults = defaults_cache[defaults_key]
            factor = flt(defaults.get("ct_conversion_factor")) or 1.0
            detail_values = {
                "item_group": defaults.get("item_group"),
                "item_code": defaults.get("item_code"),
                "item_name": defaults.get("item_name"),
                "stock_uom": defaults.get("stock_uom"),
                "warehouse": defaults.get("warehouse"),
                "quality_status": quality_status or "A",
                "stock_account": defaults.get("stock_account"),
                "stock_balance": 0,
                "ct_conversion_factor": factor,
                "qty_in_ct": 0,
                "valuation_rate": 0,
                "stock_valuation": 0,
                "manual_valuation_required": 0,
            }

        adjusted_physical_count_ct = flt(adjusted_physical_qty / factor)

        source_json = _build_roll_forward_source_json(
            source_inventory_session=source.name,
            historical_inventory_date=historical_inventory_date,
            target_inventory_date=target_inventory_date,
            historical_physical=historical_physical_entry,
            historical_system=historical_system_entry,
            current_system_qty=current_system_qty,
            adjusted_physical_qty=adjusted_physical_qty,
            negative_stock=bool(negative_stock),
        )

        detail_values.update(
            {
                "physical_count": adjusted_physical_count_ct,
                "quantity_difference": 0,
                "physical_stock_value": flt(detail_values.get("stock_valuation")),
                "difference_amount": 0,
                "manual_entry": 0,
                "negative_stock": negative_stock,
                "source_inventory_json": frappe.as_json(source_json),
            }
        )

        detail_records.append({
            "item_code": item_code,
            "warehouse": warehouse,
            "quality_status": quality_status,
            "difference_qty_stock_uom": historical_difference_qty,
            "detail_values": detail_values,
            "historical_system_qty": historical_system_qty,
            "historical_physical_qty": historical_physical_qty,
            "current_system_qty": current_system_qty,
            "adjusted_physical_qty": adjusted_physical_qty,
        })

    # Item-level zero variance, including compensating location changes.
    item_differences = {}
    warehouse_differences = {}
    for entry in detail_records:
        item = entry["item_code"]
        amount = entry["difference_qty_stock_uom"]
        item_differences[item] = item_differences.get(item, 0.0) + amount
        wh_key = (item, entry["warehouse"])
        warehouse_differences[wh_key] = warehouse_differences.get(wh_key, 0.0) + amount

    zero_items = {item for item, qty in item_differences.items() if abs(qty) <= 0.000001}
    moved_items = set()
    for item in zero_items:
        wh_deltas = [d for (i, w), d in warehouse_differences.items() if i == item]
        if any(d > 0.000001 for d in wh_deltas) and any(d < -0.000001 for d in wh_deltas):
            moved_items.add(item)

    raw_line_count = len([r for r in source.get("items") or [] if r.get("item_code")])
    duplicates_count = sum(len(row.get("duplicate_row_ids") or []) for row in physical_rows)
    summary = {
        "raw_lines": raw_line_count,
        "duplicates_removed": duplicates_count,
        "matched": sum(1 for r in physical_rows if (r.get("match") or {}).get("system")),
        "moved": sum(1 for r in physical_rows if (r.get("match") or {}).get("base_match_type") == "MOVED"),
        "ambiguous": sum(1 for r in physical_rows if (r.get("match") or {}).get("ambiguous")),
        "zero_variance_items": len(zero_items),
        "relocated_zero_variance_items": len(moved_items),
        "variance_items": len(item_differences) - len(zero_items),
        "recount_rows": negative_rows,
    }
    report = []
    for entry in detail_records:
        if entry["item_code"] not in zero_items:
            continue
        item = entry["item_code"]
        detail = entry["detail_values"]
        source_data = frappe.parse_json(detail.get("source_inventory_json") or "{}")
        origin = []
        for source_part in source_data.get("sources") or []:
            matched = source_part.get("matched_system") or {}
            wh = matched.get("warehouse")
            if wh and wh not in origin:
                origin.append(wh)
        report.append({
            "item_code": item,
            "item_name": detail.get("item_name"),
            "warehouse": entry["warehouse"],
            "previous_warehouses": origin,
            "quality_status": entry["quality_status"],
            "historical_system_qty": entry["historical_system_qty"],
            "historical_physical_qty": entry["historical_physical_qty"],
            "current_system_qty": entry["current_system_qty"],
            "adjusted_physical_qty": entry["adjusted_physical_qty"],
            "difference_qty": entry["difference_qty_stock_uom"],
            "valuation_rate_ct": flt(detail.get("valuation_rate")),
            "ct_conversion_factor": flt(detail.get("ct_conversion_factor")) or 1.0,
            "stock_value": flt(detail.get("stock_valuation")),
            "is_relocation": item in moved_items,
        })
    return {
        "source": source, "campaign": campaign, "target_date": target_inventory_date,
        "historical_date": historical_inventory_date, "warehouses": warehouses,
        "records": detail_records, "zero_items": zero_items,
        "moved_items": moved_items, "summary": summary, "report": report,
        "negative_rows": negative_rows,
    }


def _prepare_inventory_session_rows(source):
    """Return deduplicated physical rows and a unique system occurrence pool."""

    physical_rows = []
    physical_index = {}
    system_pool = []
    system_index = {}

    for row in source.get("items") or []:
        item_code = str(row.get("item_code") or "").strip()
        warehouse = str(row.get("location_warehouse") or "").strip()
        if not item_code or not warehouse:
            continue

        qty_usable = flt(row.get("qty_usable"))
        qty_damaged = flt(row.get("qty_damaged"))
        qty_obsolete = flt(row.get("qty_obsolete"))
        qty_to_verify = flt(row.get("qty_to_verify"))
        total_qty = qty_usable + qty_damaged + qty_obsolete + qty_to_verify

        if total_qty <= 0:
            continue

        # "Strict duplicate": same Item, same physical Warehouse and same complete
        # quantity breakdown. This avoids discarding a row whose total happens to
        # be equal but whose quality-state composition is genuinely different.
        duplicate_key = (
            item_code,
            warehouse,
            _qty_key(qty_usable),
            _qty_key(qty_damaged),
            _qty_key(qty_obsolete),
            _qty_key(qty_to_verify),
        )

        if duplicate_key in physical_index:
            physical_index[duplicate_key]["duplicate_row_ids"].append(row.name)
        else:
            physical = {
                "row_id": row.name,
                "item_code": item_code,
                "warehouse": warehouse,
                "qty_usable": qty_usable,
                "qty_damaged": qty_damaged,
                "qty_obsolete": qty_obsolete,
                "qty_to_verify": qty_to_verify,
                "total_qty": total_qty,
                "duplicate_row_ids": [],
                "match": None,
            }
            physical_rows.append(physical)
            physical_index[duplicate_key] = physical

        snapshot = _parse_system_stock_json(row.get("system_stock_json"))
        for system_row in snapshot:
            system_warehouse = str(system_row.get("warehouse") or "").strip()
            status = str(system_row.get("status") or "A").strip() or "A"
            qty = flt(system_row.get("qty"))
            if not system_warehouse or qty <= 0:
                continue

            # system_stock_json is a repeated Item-level snapshot. The same
            # occurrence appearing in many mobile rows must exist only once.
            system_key = (
                item_code,
                system_warehouse,
                status,
                _qty_key(qty),
            )
            if system_key in system_index:
                system_index[system_key]["source_row_ids"].append(row.name)
                continue

            occurrence = {
                "id": len(system_pool) + 1,
                "item_code": item_code,
                "warehouse": system_warehouse,
                "status": status,
                "qty": qty,
                "consumed": False,
                "source_row_ids": [row.name],
            }
            system_pool.append(occurrence)
            system_index[system_key] = occurrence

    return physical_rows, system_pool


def _match_physical_rows_to_system(physical_rows, system_pool):
    """Match globally per Item using strongest matches first.

    Pass 1: same Item + Qty + Warehouse.
    Pass 2: same Item + Qty, Warehouse may have moved.

    Because every candidate inside one pass has the same quantity and each system
    occurrence is consumed once, this produces the maximum number of exact-qty
    matches while preserving same-Warehouse matches before moved-stock matches.
    """

    items = sorted({row["item_code"] for row in physical_rows})

    for item_code in items:
        item_physical = [row for row in physical_rows if row["item_code"] == item_code]
        item_system = [row for row in system_pool if row["item_code"] == item_code]

        item_physical.sort(key=lambda row: (row["warehouse"], row["row_id"]))

        # First consume same Warehouse + same Qty.
        for physical in item_physical:
            candidates = [
                system for system in item_system
                if not system["consumed"]
                and system["warehouse"] == physical["warehouse"]
                and _same_qty(system["qty"], physical["total_qty"])
            ]
            _consume_best_candidate(physical, candidates, "EXACT")

        # Then consume same Qty elsewhere: this represents a probable location move.
        for physical in item_physical:
            if physical.get("match"):
                continue
            candidates = [
                system for system in item_system
                if not system["consumed"]
                and _same_qty(system["qty"], physical["total_qty"])
            ]
            _consume_best_candidate(physical, candidates, "MOVED")

        for physical in item_physical:
            if not physical.get("match"):
                physical["match"] = {
                    "match_type": "UNMATCHED_PHYSICAL",
                    "ambiguous": False,
                    "candidate_count": 0,
                    "system": None,
                }


def _consume_best_candidate(physical, candidates, match_type):
    if not candidates:
        return

    candidates.sort(key=_system_candidate_sort_key)
    selected = candidates[0]
    selected["consumed"] = True

    physical["match"] = {
        "match_type": match_type if len(candidates) == 1 else "AMBIGUOUS",
        "base_match_type": match_type,
        "ambiguous": len(candidates) > 1,
        "candidate_count": len(candidates),
        "system": {
            "warehouse": selected["warehouse"],
            "status": selected["status"],
            "qty": selected["qty"],
            "occurrence_id": selected["id"],
        },
    }


def _system_candidate_sort_key(system):
    # When quantity/location evidence cannot distinguish several candidates,
    # keep the choice deterministic without assigning business priority to a
    # particular Quality Status. The ambiguity is retained in source_inventory_json.
    return (
        int(system.get("id") or 0),
        str(system.get("warehouse") or ""),
        str(system.get("status") or ""),
    )


def _build_clean_physical_rows(source_inventory_session, physical_rows):
    """Transform matched physical rows into Item/Warehouse/Quality quantities."""

    aggregated = {}

    for physical in physical_rows:
        match = physical.get("match") or {}
        matched_system = match.get("system") or {}
        usable_status = str(matched_system.get("status") or "A").strip() or "A"

        components = []
        if flt(physical.get("qty_usable")) > 0:
            components.append(("qty_usable", usable_status, flt(physical["qty_usable"])))

        rejected_qty = flt(physical.get("qty_damaged")) + flt(physical.get("qty_obsolete"))
        if rejected_qty > 0:
            components.append(("qty_damaged_obsolete", "R", rejected_qty))

        if flt(physical.get("qty_to_verify")) > 0:
            components.append(("qty_to_verify", "Q", flt(physical["qty_to_verify"])))

        for component, quality_status, qty in components:
            key = (
                physical["item_code"],
                physical["warehouse"],
                quality_status,
            )
            if key not in aggregated:
                aggregated[key] = {
                    "item_code": physical["item_code"],
                    "warehouse": physical["warehouse"],
                    "quality_status": quality_status,
                    "qty": 0.0,
                    "sources": [],
                }

            aggregated[key]["qty"] += qty
            aggregated[key]["sources"].append(
                {
                    "inventory_session": source_inventory_session,
                    "row_id": physical["row_id"],
                    "duplicate_row_ids": physical.get("duplicate_row_ids") or [],
                    "component": component,
                    "qty": qty,
                    "physical_total_qty": flt(physical.get("total_qty")),
                    "match_type": match.get("match_type"),
                    "base_match_type": match.get("base_match_type"),
                    "ambiguous": bool(match.get("ambiguous")),
                    "candidate_count": int(match.get("candidate_count") or 0),
                    "matched_system": matched_system or None,
                }
            )

    return sorted(
        aggregated.values(),
        key=lambda row: (row["item_code"], row["warehouse"], row["quality_status"]),
    )



def _build_historical_physical_by_key(clean_rows):
    """Return cleaned historical physical quantity/evidence by final stock key."""

    result = {}
    for row in clean_rows:
        key = (
            row.get("item_code"),
            row.get("warehouse"),
            row.get("quality_status") or "A",
        )
        result[key] = {
            "qty": flt(row.get("qty")),
            "sources": row.get("sources") or [],
        }
    return result


def _build_historical_system_by_key(system_pool):
    """Return the deduplicated historical system snapshot by stock key.

    system_stock_json can be repeated on many mobile rows.  _prepare_inventory_session_rows
    already removes exact repeated occurrences; here we aggregate the remaining
    unique occurrences for Item + Warehouse + Quality Status.
    """

    result = {}
    for row in system_pool:
        key = (
            row.get("item_code"),
            row.get("warehouse"),
            row.get("status") or "A",
        )
        if key not in result:
            result[key] = {
                "qty": 0.0,
                "occurrences": [],
            }

        result[key]["qty"] += flt(row.get("qty"))
        result[key]["occurrences"].append(
            {
                "occurrence_id": row.get("id"),
                "warehouse": row.get("warehouse"),
                "status": row.get("status") or "A",
                "qty": flt(row.get("qty")),
                "consumed_by_matching": bool(row.get("consumed")),
                "source_row_ids": row.get("source_row_ids") or [],
            }
        )

    return result


def _build_roll_forward_source_json(
    source_inventory_session,
    historical_inventory_date,
    target_inventory_date,
    historical_physical,
    historical_system,
    current_system_qty,
    adjusted_physical_qty,
    negative_stock,
):
    historical_physical_qty = flt((historical_physical or {}).get("qty"))
    historical_system_qty = flt((historical_system or {}).get("qty"))
    historical_difference_qty = flt(
        historical_physical_qty - historical_system_qty
    )

    payload = {
        "source_inventory_session": source_inventory_session,
        "historical_inventory_date": str(historical_inventory_date),
        "target_inventory_date": str(target_inventory_date),
        "sources": (historical_physical or {}).get("sources") or [],
        "historical": {
            "system_qty_stock_uom": historical_system_qty,
            "physical_qty_stock_uom": historical_physical_qty,
            "difference_qty_stock_uom": historical_difference_qty,
            "system_occurrences": (historical_system or {}).get("occurrences") or [],
        },
        "roll_forward": {
            "current_system_qty_stock_uom": flt(current_system_qty),
            "system_change_qty_stock_uom": flt(
                current_system_qty - historical_system_qty
            ),
            "adjusted_physical_qty_stock_uom": flt(adjusted_physical_qty),
            "negative_stock": bool(negative_stock),
            "recount_required": bool(negative_stock),
        },
    }

    if negative_stock:
        payload["roll_forward"]["reason"] = "NEGATIVE_ADJUSTED_PHYSICAL_COUNT"

    return payload

def _collect_target_warehouses(physical_rows, system_pool):
    warehouses = set()

    for row in physical_rows:
        if row.get("warehouse"):
            warehouses.add(row["warehouse"])

    for row in system_pool:
        if row.get("warehouse"):
            warehouses.add(row["warehouse"])

    return sorted(warehouses)


def _get_unmatched_system_by_key(system_pool):
    result = {}
    for row in system_pool:
        if row.get("consumed"):
            continue
        key = (
            row.get("item_code"),
            row.get("warehouse"),
            row.get("status") or "A",
        )
        result.setdefault(key, []).append(
            {
                "warehouse": row.get("warehouse"),
                "status": row.get("status") or "A",
                "qty": flt(row.get("qty")),
                "occurrence_id": row.get("id"),
            }
        )
    return result


def _build_source_json(source_inventory_session, clean=None, unmatched_system=None):
    payload = {
        "source_inventory_session": source_inventory_session,
        "sources": clean.get("sources") if clean else [],
    }

    if unmatched_system:
        payload["unmatched_system"] = unmatched_system

    if clean:
        payload["clean_physical_qty_stock_uom"] = flt(clean.get("qty"))

    return payload


def _parse_system_stock_json(value):
    if not value:
        return []

    try:
        parsed = frappe.parse_json(value) if isinstance(value, str) else value
    except Exception:
        return []

    if isinstance(parsed, list):
        return [row for row in parsed if isinstance(row, dict)]

    # Legacy shape support: do not invent Warehouse rows when the old payload has
    # only totals by status. Matching needs explicit warehouse/status/qty tuples.
    return []


def _qty_key(value):
    return round(flt(value), 6)


def _same_qty(left, right):
    return abs(flt(left) - flt(right)) <= 0.000001


def _get_mobile_detail_defaults(item_code: str, warehouse: str, quality_status: str) -> dict:
    """Metadata for missing ERP snapshot combinations, without manual-session locks."""
    item = frappe.db.get_value(
        "Item", item_code, ["item_group", "item_name", "stock_uom"], as_dict=True
    )
    warehouse_info = frappe.db.get_value(
        "Warehouse", warehouse, ["account", "parent_warehouse"], as_dict=True
    )
    if not item or not warehouse_info:
        frappe.throw("Missing Item/Warehouse master data: {0} / {1}".format(item_code, warehouse))
    account = warehouse_info.account
    if not account and warehouse_info.parent_warehouse:
        account = frappe.db.get_value("Warehouse", warehouse_info.parent_warehouse, "account")
    factor = frappe.db.get_value(
        "UOM Conversion Detail",
        {"parent": item_code, "parenttype": "Item", "parentfield": "uoms", "uom": "CT"},
        "conversion_factor",
    )
    return {"item_group": item.item_group, "item_code": item_code,
            "item_name": item.item_name, "stock_uom": item.stock_uom,
            "warehouse": warehouse, "quality_status": quality_status,
            "stock_account": account, "ct_conversion_factor": flt(factor) or 1.0}
