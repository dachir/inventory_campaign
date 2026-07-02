# inventory_campaign/inventory_campaign/api/session.py

"""
Sprint 3 - Inventory Session submission API.

The mobile app counts locally and submits a complete session only at closure.
ERPNext creates the Inventory Session after validating the temporary mobile
credential, the Inventory Agent scope, authorized items, and authorized
locations.

Important MVP rules enforced here:
- mobile_session_id is the idempotency key;
- only authorized/planned items become Inventory Session Item rows;
- unplanned items/warehouses remain JSON evidence on Inventory Session;
- optional recoding is stored as a proposal on Inventory Session Item;
- the API never creates Item, Warehouse, Item Group, Famille, or Category master data;
- the API returns a server ACK that the mobile may use before purging local data.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

import frappe
from frappe.utils import cint, flt, get_datetime, now_datetime
from frappe.utils.file_manager import save_file

from inventory_campaign.api.error_reporting import error_response
from inventory_campaign.utils.quality_stock import (
    get_quality_status_stock_snapshot,
    get_system_qty_from_snapshot,
    is_warehouse_descendant_or_self,
)


SUBMIT_PROTOCOL = "inventory_campaign_session_submit_v1"
ALLOWED_SESSION_STATUSES_FOR_IDEMPOTENT_ACK = {"Submitted", "Imported", "Rejected", "Cancelled", "Failed"}
DATA_IMAGE_PATTERN = re.compile(r"^data:(image/(?:jpeg|jpg|png|webp));base64,(.+)$", re.IGNORECASE | re.DOTALL)
PHOTO_FIELDNAMES = ("photo_1", "photo_2", "photo_3")


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



def _iso_datetime(value: Any) -> str | None:
    if not value:
        return None
    try:
        return get_datetime(value).isoformat()
    except Exception:
        return str(value)



def _as_list(value: Any) -> list[Any]:
    value = _json_loads(value)
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        # Accept common wrapper shapes while keeping one-object payloads usable.
        for key in ("items", "rows", "data", "values", "locations", "warehouses"):
            inner = value.get(key)
            if isinstance(inner, list):
                return inner
        return [value]
    return []



def _hash_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()



def _sanitize_photo_value_for_storage(value: Any) -> Any:
    text = _safe_str(value)
    if text and text.lower().startswith("data:image/"):
        return "[mobile_photo_payload_omitted]"
    return value


def _sanitize_payload_for_storage(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload or {})
    sanitized.pop("mobile_credential", None)
    sanitized.pop("agent_token", None)
    sanitized.pop("token", None)
    sanitized["protocol"] = sanitized.get("protocol") or SUBMIT_PROTOCOL

    # Do not store base64 image bodies inside raw_payload_json. The photos are
    # saved as ERPNext File records and linked back to Inventory Session Item.
    items = []
    for row in _as_list(sanitized.get("items")):
        if not isinstance(row, dict):
            items.append(row)
            continue
        clean_row = dict(row)
        for fieldname in PHOTO_FIELDNAMES:
            if fieldname in clean_row:
                clean_row[fieldname] = _sanitize_photo_value_for_storage(clean_row.get(fieldname))
        items.append(clean_row)
    if "items" in sanitized:
        sanitized["items"] = items

    return sanitized



def _error(reason: str, **extra: Any) -> dict[str, Any]:
    """Backward-compatible mobile error envelope.

    Existing callers may still pass only a reason. New callers should pass
    error_code/error_stage and log=True to write a structured ERPNext error log.
    """

    should_log = bool(extra.pop("log", False))
    error_code = extra.pop("error_code", None) or "INVENTORY_SESSION_ERROR"
    error_stage = extra.pop("error_stage", None) or "submit_inventory_session"
    technical_message = extra.pop("technical_message", None)
    details = extra.pop("details", None)
    payload = extra.pop("payload", None)
    traceback = extra.pop("traceback", None)

    if should_log:
        return error_response(
            reason,
            error_code=error_code,
            error_stage=error_stage,
            error_type=extra.pop("error_type", "SESSION_SUBMIT_ERROR"),
            log=True,
            technical_message=technical_message,
            details=details,
            payload=payload,
            traceback=traceback,
            **extra,
        )

    response = {
        "ok": False,
        "submitted": False,
        "ack": False,
        "mobile_can_purge": False,
        "error": True,
        "error_id": None,
        "error_code": error_code,
        "error_stage": error_stage,
        "reason": reason,
        "agent_message": reason,
        "technical_message": technical_message,
        "details": details,
        "next_step": "keep_mobile_session_and_retry",
    }
    response.update(extra)
    return response



def _has_field(doctype: str, fieldname: str) -> bool:
    try:
        return bool(frappe.get_meta(doctype).has_field(fieldname))
    except Exception:
        return False


def _get_meta_field(doctype: str, fieldname: str) -> Any | None:
    try:
        return frappe.get_meta(doctype).get_field(fieldname)
    except Exception:
        return None


def _safe_warehouse_field_value(doctype: str, fieldname: str, value: Any) -> str | None:
    """Return a value safe to assign to a Warehouse Link field.

    During the mobile inventory session, the agent may enter a terrain location
    such as "Rack 01" or "01AE05". In many existing doctypes,
    location_warehouse is still a Link to Warehouse. Assigning a non-Warehouse
    value would break insert with LinkValidationError. If the field is still a
    Warehouse Link, keep only real Warehouse names. If the field has been
    manually changed to Data, keep the terrain value.
    """

    value = _safe_str(value)
    if not value:
        return None

    field = _get_meta_field(doctype, fieldname)
    if field and field.fieldtype == "Link" and field.options == "Warehouse":
        return value if frappe.db.exists("Warehouse", value) else None

    return value


def _warehouse_location_suffix(parent_warehouse: Any) -> str:
    """Return the ERPNext company/site suffix from a Warehouse name.

    ERPNext Warehouse names usually end with the company abbreviation, for
    example ``All Warehouses - MCO`` or ``40AE05 - MCO``. The mobile user may
    type only the terrain rayon code (``40AE05``). For item rows we need to
    store the real ERPNext location warehouse, so we append the suffix from the
    campaign warehouse before assigning ``Inventory Session Item.location_warehouse``.
    """

    text = _safe_str(parent_warehouse)
    if " - " not in text:
        return ""
    suffix = text[text.rfind(" - "):]
    return suffix if suffix.strip(" -") else ""


def _with_warehouse_location_suffix(value: Any, parent_warehouse: Any) -> str:
    """Append the parent warehouse suffix to a rayon/location value if needed."""

    text = _safe_str(value)
    if not text:
        return ""

    suffix = _warehouse_location_suffix(parent_warehouse)
    if not suffix:
        return text

    if text.lower().endswith(suffix.lower()):
        return text

    # Also avoid doubling when the user entered only the suffix code without
    # spaces, e.g. ``40AE05-MCO``. Keep the user input unchanged in that case.
    compact_text = text.lower().replace(" ", "")
    compact_suffix = suffix.lower().replace(" ", "")
    if compact_text.endswith(compact_suffix):
        return text

    return f"{text}{suffix}"


def _append_terrain_note(existing_notes: str | None, location_warehouse: str | None, zone: str | None) -> str | None:
    parts = []
    existing_notes = _safe_str(existing_notes)
    if existing_notes:
        parts.append(existing_notes)

    details = []
    if _safe_str(location_warehouse):
        details.append(f"Location terrain: {_safe_str(location_warehouse)}")
    if _safe_str(zone):
        details.append(f"Zone/rayon: {_safe_str(zone)}")

    if details:
        parts.append(" | ".join(details))

    return "\n".join(parts) if parts else None




def _photo_extension_from_mime(mime_type: str) -> str:
    mime_type = (mime_type or "").lower()
    if "png" in mime_type:
        return "png"
    if "webp" in mime_type:
        return "webp"
    return "jpg"


def _normalize_photo_payload(value: Any, item_code: str, fieldname: str) -> dict[str, Any] | None:
    text = _safe_str(value)
    if not text:
        return None

    # Already-uploaded ERPNext file URLs are kept as they are.
    if text.startswith("/files/") or text.startswith("/private/files/"):
        return {"type": "url", "url": text}

    match = DATA_IMAGE_PATTERN.match(text)
    if not match:
        # Ignore unsupported local device paths/unknown strings server-side.
        # The Flutter client should send data:image/... payloads when files are local.
        return None

    mime_type = match.group(1).lower().replace("image/jpg", "image/jpeg")
    raw_base64 = match.group(2).strip()
    try:
        content = base64.b64decode(raw_base64, validate=True)
    except Exception:
        frappe.throw(f"Invalid image payload for {fieldname} on item {item_code}.")

    if not content:
        return None

    # Keep the payload reasonable. Mobile captures are already resized, but this
    # protects ERPNext from accidental huge submissions.
    max_bytes = 5 * 1024 * 1024
    if len(content) > max_bytes:
        frappe.throw(f"Image payload for {fieldname} on item {item_code} exceeds 5 MB.")

    ext = _photo_extension_from_mime(mime_type)
    safe_item_code = re.sub(r"[^A-Za-z0-9_.-]+", "-", item_code or "item").strip("-") or "item"
    safe_fieldname = fieldname.replace("_", "-")
    return {
        "type": "content",
        "content": content,
        "file_name": f"inventory-{safe_item_code}-{safe_fieldname}.{ext}",
        "mime_type": mime_type,
    }


def _attach_photos_to_child_rows(doc: Any, pending_photos: list[dict[str, Any]]) -> None:
    if not pending_photos:
        return

    changed = False
    for child, photos in zip(doc.get("items") or [], pending_photos):
        if not photos:
            continue

        for fieldname in PHOTO_FIELDNAMES:
            photo_payload = photos.get(fieldname)
            if not photo_payload:
                continue

            if photo_payload.get("type") == "url":
                setattr(child, fieldname, photo_payload.get("url"))
                changed = True
                continue

            saved = save_file(
                fname=photo_payload.get("file_name"),
                content=photo_payload.get("content"),
                dt="Inventory Session Item",
                dn=child.name,
                folder=None,
                decode=False,
                is_private=1,
            )
            setattr(child, fieldname, saved.file_url)
            changed = True

    if changed:
        doc.save(ignore_permissions=True)


# -----------------------------------------------------------------------------
# Scope/context helpers
# -----------------------------------------------------------------------------


def _verify_mobile_credential(
    mobile_credential: str | None,
    campaign: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    try:
        from inventory_campaign.api.agent import verify_mobile_credential_payload

        return verify_mobile_credential_payload(
            mobile_credential=mobile_credential,
            expected_campaign=campaign,
            expected_device_id=device_id,
        )
    except Exception:
        frappe.log_error(
            title="Inventory Campaign - verify_mobile_credential_failed",
            message=frappe.get_traceback(),
        )
        return {
            "valid": False,
            "reason": "Mobile credential validation failed because of a server error",
            "payload": None,
        }



def _get_mobile_context(
    mobile_credential: str,
    campaign: str,
    device_id: str | None = None,
) -> dict[str, Any]:
    try:
        from inventory_campaign.api.agent import get_inventory_context

        return get_inventory_context(
            mobile_credential=mobile_credential,
            campaign=campaign,
            device_id=device_id,
        )
    except Exception:
        frappe.log_error(
            title="Inventory Campaign - get_inventory_context_failed",
            message=frappe.get_traceback(),
        )
        return {
            "ok": False,
            "valid": False,
            "access_allowed": False,
            "reason": "Inventory context loading failed because of a server error",
        }



def _authorized_item_codes(context: dict[str, Any]) -> set[str]:
    # Legacy helper kept for backward compatibility. The current mobile flow no
    # longer downloads authorized_items; authorization is by Item.item_group.
    result: set[str] = set()
    for row in context.get("authorized_items") or []:
        code = _safe_str(row.get("item_code"))
        if code:
            result.add(code)
    return result


def _authorized_item_group_codes(context: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for row in context.get("authorized_item_groups") or []:
        group = _safe_str(row.get("item_group") or row.get("name") or row.get("code"))
        if group:
            result.add(group)
    return result



def _authorized_location_codes(context: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for row in context.get("authorized_locations") or []:
        location = _safe_str(row.get("location_warehouse"))
        if location:
            result.add(location)
    return result



def _campaign_from_context(context: dict[str, Any], campaign: str) -> dict[str, Any] | None:
    selected = context.get("selected_campaign") or {}
    if selected.get("name") == campaign:
        return selected

    for row in context.get("available_campaigns") or []:
        if row.get("name") == campaign:
            return row
    return None



def _get_campaign_doc(campaign: str) -> Any | None:
    if not campaign or not frappe.db.exists("Inventory Campaign", campaign):
        return None
    return frappe.get_doc("Inventory Campaign", campaign)



def _get_item_doc(item_code: str) -> Any | None:
    if not item_code:
        return None
    if not frappe.db.exists("Item", item_code):
        return None
    return frappe.get_doc("Item", item_code)



def _warehouse_parent(warehouse: str | None) -> str | None:
    warehouse = _safe_str(warehouse)
    if not warehouse:
        return None
    return frappe.db.get_value("Warehouse", warehouse, "parent_warehouse")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return flt(value)
    except Exception:
        return default


def _get_system_stock_snapshot(
    item_code: str,
    snapshot_datetime: Any,
    warehouse_scope: str,
) -> list[dict[str, Any]]:
    """Return compact ERP stock snapshot inside the session warehouse tree.

    The counted line location is kept on Inventory Session Item.location_warehouse,
    but it is not used as the stock filter. The stock filter is the session/
    campaign warehouse tree: the root warehouse itself plus all descendants.
    """

    return get_quality_status_stock_snapshot(
        item_code=item_code,
        snapshot_datetime=snapshot_datetime,
        warehouse_scope=warehouse_scope,
    )


def _normalize_count_quantities(raw_row: dict[str, Any]) -> tuple[float, float, float, float, float]:
    """Normalize the apparent-state quantities.

    Current mobile rule:
    - qty_usable
    - qty_damaged
    - qty_to_verify
    - qty_obsolete

    ``counted_qty`` is intentionally ignored and must not be negotiated with
    the mobile app anymore. The only accepted source of truth is the apparent
    state quantities; ``total_counted_qty`` is always recalculated on the server.
    """

    qty_usable = _safe_float(raw_row.get("qty_usable"))
    qty_damaged = _safe_float(raw_row.get("qty_damaged"))
    qty_to_verify = _safe_float(raw_row.get("qty_to_verify"))
    qty_obsolete = _safe_float(raw_row.get("qty_obsolete"))

    total_counted_qty = qty_usable + qty_damaged + qty_to_verify + qty_obsolete
    return qty_usable, qty_damaged, qty_to_verify, qty_obsolete, total_counted_qty


# -----------------------------------------------------------------------------
# Recoding helpers
# -----------------------------------------------------------------------------


def _normalize_recoding_tags(tags: Any) -> dict[str, Any]:
    try:
        from inventory_campaign.api.recoding import normalize_recoding_tags_value

        return normalize_recoding_tags_value(tags)
    except Exception:
        frappe.log_error(
            title="Inventory Campaign - normalize_recoding_tags_failed",
            message=frappe.get_traceback(),
        )
        return {
            "ok": False,
            "valid": False,
            "reason": "Recoding tags validation failed because of a server error",
        }



def _extract_item_recoding_tags(row: dict[str, Any]) -> Any:
    for key in ("recoding_tags_json", "recoding_tags", "recoding", "recoding_proposal"):
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return None


# -----------------------------------------------------------------------------
# Payload normalization / validation
# -----------------------------------------------------------------------------


def _coerce_payload(payload: Any = None, kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    kwargs = dict(kwargs or {})

    if payload not in (None, ""):
        parsed = _json_loads(payload)
        if not isinstance(parsed, dict):
            raise ValueError("payload must be a JSON object")
        merged = dict(parsed)
        # Explicit kwargs win over nested payload only when they are meaningful.
        for key, value in kwargs.items():
            if value not in (None, ""):
                merged[key] = value
        return merged

    return kwargs



def _normalize_unplanned_payload(value: Any) -> list[Any]:
    rows = _as_list(value)
    # Keep unplanned data as evidence. We only ensure it is JSON-serializable.
    normalized = []
    for row in rows:
        if isinstance(row, dict):
            normalized.append(row)
        else:
            normalized.append({"value": row})
    return normalized



def _normalize_locations(
    payload: dict[str, Any],
    parent_warehouse: str,
    primary_location: str | None,
    authorized_locations: set[str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize optional session locations.

    Warehouse/location selection is now done at session opening by the agent.
    It is not part of the Inventory Agent access scope anymore. Therefore this
    function must not reject a session because the location is absent from
    authorized_locations.

    If location_warehouse is still a Link to Warehouse in the DocType and the
    agent entered a free terrain value (Rack, rayon, comment), we skip the child
    location row to avoid LinkValidationError. The raw value remains preserved in
    raw_payload_json and in Inventory Session.notes.
    """

    errors: list[dict[str, Any]] = []
    locations: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_location(raw_location: str | None, raw_parent: str | None = None, notes: str | None = None) -> None:
        location = _safe_str(raw_location)
        if not location or location in seen:
            return

        safe_location = _safe_warehouse_field_value("Inventory Session Location", "location_warehouse", location)
        if not safe_location:
            # Free terrain location while child field is still Link/Warehouse.
            # Keep it only in session header notes/raw_payload.
            return

        row_parent = _safe_str(raw_parent) or parent_warehouse or _warehouse_parent(safe_location)
        safe_parent = _safe_warehouse_field_value("Inventory Session Location", "parent_warehouse", row_parent)
        seen.add(location)
        locations.append({
            "parent_warehouse": safe_parent,
            "location_warehouse": safe_location,
            "location_name": frappe.db.get_value("Warehouse", safe_location, "warehouse_name") if frappe.db.exists("Warehouse", safe_location) else safe_location,
            "notes": _safe_str(notes),
        })

    for row in _as_list(payload.get("locations")):
        if not isinstance(row, dict):
            errors.append({"check": "locations", "reason": "Location row must be a JSON object", "row": row})
            continue

        add_location(
            raw_location=row.get("location_warehouse") or row.get("warehouse") or row.get("location"),
            raw_parent=row.get("parent_warehouse"),
            notes=row.get("notes"),
        )

    add_location(primary_location, parent_warehouse)

    return locations, errors


def _normalize_items(
    payload: dict[str, Any],
    parent_warehouse: str,
    primary_location: str | None,
    authorized_items: set[str],
    authorized_item_groups: set[str],
    authorized_locations: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, float, int]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    normalized_rows: list[dict[str, Any]] = []
    total_qty = 0.0
    recoding_count = 0
    stock_snapshot_at = now_datetime()

    for index, raw_row in enumerate(_as_list(payload.get("items")), start=1):
        if not isinstance(raw_row, dict):
            errors.append({"check": "items", "row_index": index, "reason": "Item row must be a JSON object", "row": raw_row})
            continue

        item_code = _safe_str(raw_row.get("item_code") or raw_row.get("item"))
        if not item_code:
            errors.append({"check": "items", "row_index": index, "reason": "Item row is missing item_code"})
            continue

        item_doc = _get_item_doc(item_code)
        if not item_doc:
            errors.append({
                "check": "items",
                "row_index": index,
                "reason": "Item does not exist in ERPNext; submit it under unplanned_items instead.",
                "item_code": item_code,
            })
            continue

        if item_doc.disabled:
            errors.append({
                "check": "items",
                "row_index": index,
                "reason": "Item is disabled in ERPNext.",
                "item_code": item_code,
            })
            continue

        if hasattr(item_doc, "is_stock_item") and not cint(item_doc.is_stock_item):
            errors.append({
                "check": "items",
                "row_index": index,
                "reason": "Item is not a stock item.",
                "item_code": item_code,
            })
            continue

        if authorized_item_groups and item_doc.item_group not in authorized_item_groups:
            errors.append({
                "check": "items",
                "row_index": index,
                "reason": "Item Group is not authorized for this Inventory Agent.",
                "item_code": item_code,
                "item_group": item_doc.item_group,
            })
            continue

        # Backward compatibility: if a legacy context still carries explicit
        # authorized_items and no item-group scope, accept only those items.
        if not authorized_item_groups and authorized_items and item_code not in authorized_items:
            errors.append({
                "check": "items",
                "row_index": index,
                "reason": "Item is not authorized for this Inventory Agent.",
                "item_code": item_code,
            })
            continue

        qty_usable, qty_damaged, qty_to_verify, qty_obsolete, total_counted_qty = _normalize_count_quantities(raw_row)
        state_quantities = {
            "qty_usable": qty_usable,
            "qty_damaged": qty_damaged,
            "qty_to_verify": qty_to_verify,
            "qty_obsolete": qty_obsolete,
        }
        negative_quantities = {key: value for key, value in state_quantities.items() if value < 0}
        if negative_quantities:
            errors.append({
                "check": "items",
                "row_index": index,
                "reason": "Quantities by apparent state cannot be negative.",
                "item_code": item_code,
                "quantities": negative_quantities,
            })
            continue

        if total_counted_qty <= 0:
            errors.append({
                "check": "items",
                "row_index": index,
                "reason": "At least one counted quantity must be greater than zero.",
                "item_code": item_code,
            })
            continue

        raw_row_parent = _safe_str(raw_row.get("parent_warehouse")) or parent_warehouse
        raw_row_rayon = _safe_str(raw_row.get("rayon") or raw_row.get("zone"))
        row_rayon = _with_warehouse_location_suffix(raw_row_rayon, raw_row_parent)

        # The item-level rayon is the real terrain location for the counted line.
        # It must be written to Inventory Session Item.location_warehouse. Older
        # mobile payloads may still send only ``rayon`` or ``zone``; newer ones
        # also send the same value as ``location_warehouse``.
        explicit_row_location = _safe_str(raw_row.get("location_warehouse"))
        explicit_row_location = _with_warehouse_location_suffix(explicit_row_location, raw_row_parent)
        fallback_location = _safe_str(raw_row.get("warehouse") or raw_row.get("location"))
        fallback_location = _with_warehouse_location_suffix(fallback_location, raw_row_parent)

        row_location = explicit_row_location or row_rayon or fallback_location or primary_location
        row_parent = raw_row_parent or _warehouse_parent(row_location)

        # Location/warehouse is a session-level terrain choice now. It is not
        # validated against Inventory Agent.authorized_locations. If the child
        # field is still a Warehouse Link and the mobile sent a terrain value,
        # keep the value in raw_payload_json/session notes and avoid assigning it
        # to the Link field. When the suffixed rayon exists as a Warehouse, it is
        # stored normally in Inventory Session Item.location_warehouse.
        safe_row_parent = _safe_warehouse_field_value("Inventory Session Item", "parent_warehouse", row_parent)
        safe_row_location = _safe_warehouse_field_value("Inventory Session Item", "location_warehouse", row_location)

        if not safe_row_location:
            errors.append({
                "check": "items",
                "row_index": index,
                "reason": "location_warehouse must resolve to an ERPNext Warehouse to store the counted line location.",
                "item_code": item_code,
                "location_warehouse": row_location,
            })
            continue

        if not is_warehouse_descendant_or_self(safe_row_location, parent_warehouse):
            errors.append({
                "check": "items",
                "row_index": index,
                "reason": "location_warehouse must be inside the session warehouse tree.",
                "item_code": item_code,
                "location_warehouse": safe_row_location,
                "session_warehouse": parent_warehouse,
            })
            continue

        try:
            system_stock_snapshot = _get_system_stock_snapshot(
                item_code=item_doc.name,
                snapshot_datetime=stock_snapshot_at,
                warehouse_scope=parent_warehouse,
            )
            row_quality_status = _safe_str(raw_row.get("quality_status"))
            system_qty = get_system_qty_from_snapshot(system_stock_snapshot, row_quality_status)
            difference_qty = total_counted_qty - system_qty
        except Exception as exc:
            errors.append({
                "check": "system_stock",
                "row_index": index,
                "reason": "ERPNext could not calculate item stock by warehouse and quality_status from Stock Ledger Entry.",
                "item_code": item_code,
                "counted_location_warehouse": safe_row_location,
                "warehouse_scope": parent_warehouse,
                "technical_message": str(exc),
            })
            continue

        recoding_tags = _extract_item_recoding_tags(raw_row)
        recoding = _normalize_recoding_tags(recoding_tags)
        if not recoding.get("valid"):
            errors.append({
                "check": "recoding",
                "row_index": index,
                "item_code": item_code,
                "reason": recoding.get("reason") or "Invalid recoding_tags_json.",
            })
            continue

        recoding_required = bool(recoding.get("recoding_required"))
        if recoding_required:
            recoding_count += 1

        summary = recoding.get("summary") or {}
        total_qty += total_counted_qty

        normalized_rows.append({
            "item_code": item_doc.name,
            "item_name": _safe_str(raw_row.get("item_name")) or item_doc.item_name,
            "barcode": _safe_str(raw_row.get("barcode") or raw_row.get("scan_code")),
            "uom": _safe_str(raw_row.get("uom")) or item_doc.stock_uom,
            "qty_usable": qty_usable,
            "qty_damaged": qty_damaged,
            "qty_to_verify": qty_to_verify,
            "qty_obsolete": qty_obsolete,
            "total_counted_qty": total_counted_qty,
            "counted_qty": total_counted_qty,
            "system_qty": system_qty,
            "difference_qty": difference_qty,
            "system_stock_json": _json_dumps(system_stock_snapshot),
            "scan_count": _safe_int(raw_row.get("scan_count"), 1),
            "last_scanned_at": raw_row.get("last_scanned_at"),
            "manual_entry": cint(raw_row.get("manual_entry") or 0),
            "parent_warehouse": safe_row_parent,
            "location_warehouse": safe_row_location,
            "rayon": row_rayon,
            "zone": row_rayon,
            "mobile_line_id": _safe_str(raw_row.get("mobile_line_id") or raw_row.get("line_id")),
            "__photos": {
                fieldname: _normalize_photo_payload(raw_row.get(fieldname), item_doc.name, fieldname)
                for fieldname in PHOTO_FIELDNAMES
                if raw_row.get(fieldname)
            },
            "recoding_required": 1 if recoding_required else 0,
            "recoding_status": recoding.get("recoding_status") or ("Pending Review" if recoding_required else "Not Required"),
            "recoding_tags_json": recoding.get("recoding_tags_json") if recoding_required else "{}",
            "recoding_famille_code": summary.get("famille_code"),
            "recoding_famille_description": summary.get("famille_description"),
            "recoding_sous_famille_code": summary.get("sous_famille_code"),
            "recoding_sous_famille_description": summary.get("sous_famille_description"),
            "recoding_caracteristique_majeure_code": summary.get("caracteristique_majeure_code"),
            "recoding_caracteristique_majeure": summary.get("caracteristique_majeure"),
            "recoding_plant_floor": summary.get("plant_floor"),
            "recoding_plant_floor_description": summary.get("plant_floor_description"),
            "recoding_workstation": summary.get("workstation"),
            "recoding_workstation_description": summary.get("workstation_description"),
            "recoding_note": _safe_str(raw_row.get("recoding_note")),
            "notes": _safe_str(raw_row.get("notes") or raw_row.get("note") or raw_row.get("comment")),
        })

        if item_doc.disabled:
            warnings.append({
                "check": "items",
                "row_index": index,
                "item_code": item_doc.name,
                "reason": "Item is disabled in ERPNext but was accepted because it is in the authorized_items scope.",
            })

    return normalized_rows, errors, len(normalized_rows), total_qty, recoding_count


# -----------------------------------------------------------------------------
# Persistence helpers
# -----------------------------------------------------------------------------


def _find_existing_session(mobile_session_id: str) -> dict[str, Any] | None:
    if not mobile_session_id:
        return None

    fields = ["name", "status", "campaign", "inventory_agent", "server_ack_at"]
    if _has_field("Inventory Session", "submit_payload_hash"):
        fields.insert(4, "submit_payload_hash")

    return frappe.db.get_value(
        "Inventory Session",
        {"mobile_session_id": mobile_session_id},
        fields,
        as_dict=True,
    )



def _touch_existing_session_retry(existing_name: str) -> None:
    try:
        current_retry_count = _safe_int(frappe.db.get_value("Inventory Session", existing_name, "submit_retry_count"), 0)
        frappe.db.set_value(
            "Inventory Session",
            existing_name,
            {
                "submit_retry_count": current_retry_count + 1,
                "server_ack_at": now_datetime(),
            },
            update_modified=False,
        )
        frappe.db.commit()
    except Exception:
        # Idempotent ACK must not fail only because retry metadata could not be updated.
        frappe.log_error(
            title="Inventory Campaign - existing_session_retry_touch_failed",
            message=frappe.get_traceback(),
        )



def _refresh_campaign_summary(campaign: str) -> None:
    if not campaign or not frappe.db.exists("Inventory Campaign", campaign):
        return

    try:
        sessions = frappe.get_all(
            "Inventory Session",
            filters={"campaign": campaign},
            fields=[
                "name",
                "status",
                "total_items_counted",
                "unplanned_items_count",
                "unplanned_warehouses_count",
            ],
        )

        total_sessions = len(sessions)
        total_items_counted = sum(_safe_int(row.get("total_items_counted"), 0) for row in sessions)
        total_unplanned_items = sum(_safe_int(row.get("unplanned_items_count"), 0) for row in sessions)
        total_unplanned_warehouses = sum(_safe_int(row.get("unplanned_warehouses_count"), 0) for row in sessions)

        frappe.db.set_value(
            "Inventory Campaign",
            campaign,
            {
                "total_sessions": total_sessions,
                "total_items_counted": total_items_counted,
                "total_unplanned_items": total_unplanned_items,
                "total_unplanned_warehouses": total_unplanned_warehouses,
            },
            update_modified=False,
        )
    except Exception:
        frappe.log_error(
            title="Inventory Campaign - campaign_summary_refresh_failed",
            message=frappe.get_traceback(),
        )



def _create_inventory_session_doc(
    payload: dict[str, Any],
    sanitized_payload: dict[str, Any],
    payload_hash: str,
    context: dict[str, Any],
    campaign_doc: Any,
    normalized_locations: list[dict[str, Any]],
    normalized_items: list[dict[str, Any]],
    unplanned_items: list[Any],
    unplanned_warehouses: list[Any],
    total_items_counted: int,
    total_qty_counted: float,
    recoding_proposals_count: int,
) -> Any:
    credential_payload = context.get("mobile_credential_payload") or {}
    agent_ctx = context.get("inventory_agent") or {}

    mobile_session_id = _safe_str(payload.get("mobile_session_id"))
    parent_warehouse = _safe_str(payload.get("parent_warehouse")) or campaign_doc.warehouse
    location_warehouse = _safe_str(payload.get("location_warehouse"))
    zone = _safe_str(payload.get("zone"))
    device_id = _safe_str(payload.get("device_id")) or _safe_str(credential_payload.get("device_id"))

    safe_parent_warehouse = _safe_warehouse_field_value("Inventory Session", "parent_warehouse", parent_warehouse)
    safe_warehouse = _safe_warehouse_field_value("Inventory Session", "warehouse", parent_warehouse)
    safe_location_warehouse = _safe_warehouse_field_value("Inventory Session", "location_warehouse", location_warehouse)
    session_notes = _append_terrain_note(_safe_str(payload.get("notes")), location_warehouse, zone)

    doc_data = {
        "doctype": "Inventory Session",
        "campaign": campaign_doc.name,
        "mobile_session_id": mobile_session_id,
        "status": "Submitted",
        "inventory_agent": agent_ctx.get("name") or credential_payload.get("inventory_agent"),
        "operator_user": _safe_str(payload.get("operator_user")),
        "operator_name": _safe_str(payload.get("operator_name")) or agent_ctx.get("agent_name"),
        "device_id": device_id,
        "company": campaign_doc.company,
        "warehouse": safe_warehouse,
        "parent_warehouse": safe_parent_warehouse,
        "location_warehouse": safe_location_warehouse,
        "zone": zone,
        "item_group": _safe_str(payload.get("item_group")),
        "opened_at": payload.get("opened_at"),
        "closed_at": payload.get("closed_at"),
        "submitted_at": payload.get("submitted_at") or now_datetime(),
        "server_ack_at": now_datetime(),
        "submitted_from_mobile": 1,
        "submit_retry_count": _safe_int(payload.get("submit_retry_count"), 0),
        "unplanned_items_json": _json_dumps(unplanned_items),
        "unplanned_warehouses_json": _json_dumps(unplanned_warehouses),
        "has_unplanned_items": 1 if unplanned_items else 0,
        "unplanned_items_count": len(unplanned_items),
        "has_unplanned_warehouses": 1 if unplanned_warehouses else 0,
        "unplanned_warehouses_count": len(unplanned_warehouses),
        "has_recoding_proposals": 1 if recoding_proposals_count else 0,
        "recoding_proposals_count": recoding_proposals_count,
        "review_status": "Pending",
        "total_items_counted": total_items_counted,
        "total_qty_counted": total_qty_counted,
        "raw_payload_json": _json_dumps(sanitized_payload),
        "notes": session_notes,
    }

    if _has_field("Inventory Session", "branch"):
        doc_data["branch"] = _safe_str(payload.get("branch")) or _safe_str(payload.get("site"))

    if _has_field("Inventory Session", "submit_payload_hash"):
        doc_data["submit_payload_hash"] = payload_hash

    doc = frappe.get_doc(doc_data)

    for location_row in normalized_locations:
        doc.append("locations", location_row)

    pending_photos: list[dict[str, Any]] = []
    for item_row in normalized_items:
        clean_item_row = dict(item_row)
        pending_photos.append(clean_item_row.pop("__photos", {}) or {})
        doc.append("items", clean_item_row)

    doc.insert(ignore_permissions=True)
    _attach_photos_to_child_rows(doc, pending_photos)

    if not cint(getattr(doc, "docstatus", 0)):
        if not cint(getattr(doc.meta, "is_submittable", 0)):
            frappe.throw("Inventory Session must be submittable to accept mobile submissions.")

        doc.flags.ignore_permissions = True
        doc.submit()

    return doc


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def submit_inventory_session(
    mobile_credential: str | None = None,
    payload: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """
    Submit a closed mobile inventory session to ERPNext.

    The API is deliberately idempotent. If the same mobile_session_id is sent
    again with the same payload hash, the existing Inventory Session is returned
    as a duplicate ACK and the mobile may still purge its local data.
    """

    try:
        incoming_payload = _coerce_payload(payload=payload, kwargs=kwargs)
    except Exception as exc:
        return _error(
            "ERPNext n’a pas pu lire les données envoyées par le mobile.",
            log=True,
            error_code="PAYLOAD_PARSE_FAILED",
            error_stage="payload_parse",
            technical_message=str(exc),
            payload={"payload": payload, "kwargs": kwargs},
            traceback=frappe.get_traceback(),
        )

    mobile_credential = _safe_str(mobile_credential) or _safe_str(incoming_payload.get("mobile_credential"))
    campaign = _safe_str(incoming_payload.get("campaign"))
    mobile_session_id = _safe_str(incoming_payload.get("mobile_session_id"))
    device_id = _safe_str(incoming_payload.get("device_id"))

    if not mobile_session_id:
        return _error(
            "Identifiant de session mobile manquant. Fermez et rouvrez la session puis réessayez.",
            log=True,
            error_code="MOBILE_SESSION_ID_MISSING",
            error_stage="payload_validation",
            campaign=campaign,
            device_id=device_id,
            payload=incoming_payload,
        )

    if not campaign:
        return _error(
            "Campagne ERPNext manquante dans la session mobile.",
            log=True,
            error_code="CAMPAIGN_MISSING",
            error_stage="payload_validation",
            mobile_session_id=mobile_session_id,
            device_id=device_id,
            payload=incoming_payload,
        )

    if not mobile_credential:
        return _error(
            "Credential mobile manquant. L’agent doit rescanner le QR de connexion.",
            log=True,
            error_code="MOBILE_CREDENTIAL_MISSING",
            error_stage="credential_validation",
            campaign=campaign,
            mobile_session_id=mobile_session_id,
            device_id=device_id,
            payload=incoming_payload,
        )

    sanitized_payload = _sanitize_payload_for_storage(incoming_payload)
    payload_hash = _hash_payload(sanitized_payload)

    existing = _find_existing_session(mobile_session_id)
    if existing:
        existing_hash = _safe_str(existing.get("submit_payload_hash"))
        if existing_hash and existing_hash != payload_hash:
            return _error(
                "Cette session mobile existe déjà dans ERPNext avec un contenu différent. Ne purgez pas le téléphone et contactez le support.",
                log=True,
                error_code="MOBILE_SESSION_DUPLICATE_CONFLICT",
                error_stage="idempotency_check",
                campaign=campaign,
                mobile_session_id=mobile_session_id,
                device_id=device_id,
                payload=sanitized_payload,
                details={
                    "existing_inventory_session": existing.get("name"),
                    "existing_status": existing.get("status"),
                },
                conflict=True,
                duplicate=True,
                existing_inventory_session=existing.get("name"),
                existing_status=existing.get("status"),
            )

        _touch_existing_session_retry(existing.get("name"))
        return {
            "ok": True,
            "submitted": True,
            "ack": True,
            "duplicate": True,
            "idempotent_ack": True,
            "reason": "Inventory Session was already submitted. Existing session returned.",
            "inventory_session": existing.get("name"),
            "status": existing.get("status"),
            "campaign": existing.get("campaign"),
            "inventory_agent": existing.get("inventory_agent"),
            "mobile_session_id": mobile_session_id,
            "server_ack_at": _iso_datetime(now_datetime()),
            "mobile_can_purge": True,
        }

    verification = _verify_mobile_credential(
        mobile_credential=mobile_credential,
        campaign=campaign,
        device_id=device_id,
    )
    if not verification.get("valid"):
        return _error(
            "Credential mobile refusé par ERPNext. L’agent doit rescanner le QR de connexion.",
            log=True,
            error_code="MOBILE_CREDENTIAL_INVALID",
            error_stage="credential_validation",
            technical_message=verification.get("reason") or "Mobile credential is invalid.",
            campaign=campaign,
            mobile_session_id=mobile_session_id,
            device_id=device_id,
            payload=sanitized_payload,
            valid=False,
        )

    context = _get_mobile_context(
        mobile_credential=mobile_credential,
        campaign=campaign,
        device_id=device_id,
    )
    if not context.get("ok") or not context.get("access_allowed"):
        return _error(
            "Accès agent refusé par ERPNext. Vérifiez le statut agent, les groupes d’articles et les magasins autorisés.",
            log=True,
            error_code="AGENT_CONTEXT_DENIED",
            error_stage="agent_context_validation",
            technical_message=context.get("reason") or "Inventory Agent context is not allowed.",
            campaign=campaign,
            mobile_session_id=mobile_session_id,
            device_id=device_id,
            payload=sanitized_payload,
            details=context,
            valid=False,
        )

    campaign_context = _campaign_from_context(context, campaign)

    campaign_doc = _get_campaign_doc(campaign)
    if not campaign_doc:
        return _error(
            "La campagne d’inventaire n’existe pas dans ERPNext.",
            log=True,
            error_code="CAMPAIGN_NOT_FOUND",
            error_stage="campaign_validation",
            campaign=campaign,
            mobile_session_id=mobile_session_id,
            device_id=device_id,
            payload=sanitized_payload,
        )

    if campaign_doc.status not in {"Draft", "Open"}:
        return _error(
            "La campagne ERPNext n’est pas ouverte. La session mobile ne peut pas être reçue.",
            log=True,
            error_code="CAMPAIGN_NOT_OPEN",
            error_stage="campaign_validation",
            campaign=campaign,
            mobile_session_id=mobile_session_id,
            device_id=device_id,
            payload=sanitized_payload,
            details={"campaign_status": campaign_doc.status},
            status=campaign_doc.status,
        )

    agent_ctx = context.get("inventory_agent") or {}
    agent_company = _safe_str(agent_ctx.get("company"))
    if agent_company and campaign_doc.company != agent_company:
        return _error(
            "La société de la campagne ne correspond pas à la société de l’agent.",
            log=True,
            error_code="COMPANY_MISMATCH",
            error_stage="agent_campaign_validation",
            campaign=campaign,
            inventory_agent=_safe_str(agent_ctx.get("name")) or _safe_str(agent_ctx.get("inventory_agent")),
            mobile_session_id=mobile_session_id,
            device_id=device_id,
            payload=sanitized_payload,
            details={"campaign_company": campaign_doc.company, "agent_company": agent_company},
            campaign_company=campaign_doc.company,
            agent_company=agent_company,
        )

    incoming_branch = _safe_str(incoming_payload.get("branch")) or _safe_str(incoming_payload.get("site"))
    campaign_branch = _safe_str(getattr(campaign_doc, "branch", None)) if _has_field("Inventory Campaign", "branch") else None
    if incoming_branch and campaign_branch and incoming_branch != campaign_branch:
        return _error(
            "Le site sélectionné sur le mobile ne correspond pas au site de la campagne ERPNext.",
            log=True,
            error_code="BRANCH_MISMATCH",
            error_stage="branch_validation",
            campaign=campaign,
            mobile_session_id=mobile_session_id,
            device_id=device_id,
            payload=sanitized_payload,
            details={"campaign_branch": campaign_branch, "selected_branch": incoming_branch},
            campaign_branch=campaign_branch,
            selected_branch=incoming_branch,
        )

    authorized_items = _authorized_item_codes(context)
    authorized_item_groups = _authorized_item_group_codes(context)
    authorized_locations = _authorized_location_codes(context)

    if not authorized_item_groups and not authorized_items:
        return _error(
            "L’agent n’a aucun groupe d’articles autorisé. Impossible de recevoir la session.",
            log=True,
            error_code="AGENT_SCOPE_EMPTY",
            error_stage="agent_scope_validation",
            campaign=campaign,
            mobile_session_id=mobile_session_id,
            device_id=device_id,
            payload=sanitized_payload,
            details={"authorized_item_groups": authorized_item_groups, "authorized_items": authorized_items},
        )

    parent_warehouse = _safe_str(incoming_payload.get("parent_warehouse")) or campaign_doc.warehouse
    if parent_warehouse != campaign_doc.warehouse:
        return _error(
            "Le magasin de la session mobile ne correspond pas au magasin de la campagne ERPNext.",
            log=True,
            error_code="WAREHOUSE_MISMATCH",
            error_stage="warehouse_validation",
            campaign=campaign,
            mobile_session_id=mobile_session_id,
            device_id=device_id,
            payload=sanitized_payload,
            details={"parent_warehouse": parent_warehouse, "campaign_warehouse": campaign_doc.warehouse},
            parent_warehouse=parent_warehouse,
            campaign_warehouse=campaign_doc.warehouse,
        )

    primary_location = _safe_str(incoming_payload.get("location_warehouse"))
    normalized_locations, location_errors = _normalize_locations(
        payload=incoming_payload,
        parent_warehouse=parent_warehouse,
        primary_location=primary_location,
        authorized_locations=authorized_locations,
    )

    normalized_items, item_errors, total_items_counted, total_qty_counted, recoding_proposals_count = _normalize_items(
        payload=incoming_payload,
        parent_warehouse=parent_warehouse,
        primary_location=primary_location,
        authorized_items=authorized_items,
        authorized_item_groups=authorized_item_groups,
        authorized_locations=authorized_locations,
    )

    unplanned_items = _normalize_unplanned_payload(
        incoming_payload.get("unplanned_items")
        if "unplanned_items" in incoming_payload
        else incoming_payload.get("unplanned_items_json")
    )
    unplanned_warehouses = _normalize_unplanned_payload(
        incoming_payload.get("unplanned_warehouses")
        if "unplanned_warehouses" in incoming_payload
        else incoming_payload.get("unplanned_warehouses_json")
    )

    errors = location_errors + item_errors
    if errors:
        return _error(
            "ERPNext a refusé certaines lignes de la session. Vérifiez les articles, rayons et magasins autorisés.",
            log=True,
            error_code="SESSION_PAYLOAD_VALIDATION_FAILED",
            error_stage="line_validation",
            campaign=campaign,
            mobile_session_id=mobile_session_id,
            device_id=device_id,
            payload=sanitized_payload,
            details=errors,
            errors=errors,
        )

    if not normalized_items and not unplanned_items and not unplanned_warehouses:
        return _error(
            "La session ne contient aucune ligne comptée. Elle n’a pas été créée dans ERPNext.",
            log=True,
            error_code="EMPTY_SESSION",
            error_stage="payload_validation",
            campaign=campaign,
            mobile_session_id=mobile_session_id,
            device_id=device_id,
            payload=sanitized_payload,
        )

    try:
        doc = _create_inventory_session_doc(
            payload=incoming_payload,
            sanitized_payload=sanitized_payload,
            payload_hash=payload_hash,
            context=context,
            campaign_doc=campaign_doc,
            normalized_locations=normalized_locations,
            normalized_items=normalized_items,
            unplanned_items=unplanned_items,
            unplanned_warehouses=unplanned_warehouses,
            total_items_counted=total_items_counted,
            total_qty_counted=total_qty_counted,
            recoding_proposals_count=recoding_proposals_count,
        )
        _refresh_campaign_summary(campaign_doc.name)
        frappe.db.commit()
    except Exception as exc:
        # Duplicate race: another request may have inserted the same unique
        # mobile_session_id after our initial lookup.
        existing_after_error = _find_existing_session(mobile_session_id)
        if existing_after_error:
            frappe.db.rollback()
            _touch_existing_session_retry(existing_after_error.get("name"))
            return {
                "ok": True,
                "submitted": True,
                "ack": True,
                "duplicate": True,
                "idempotent_ack": True,
                "reason": "Inventory Session was already submitted during retry. Existing session returned.",
                "inventory_session": existing_after_error.get("name"),
                "status": existing_after_error.get("status"),
                "campaign": existing_after_error.get("campaign"),
                "inventory_agent": existing_after_error.get("inventory_agent"),
                "mobile_session_id": mobile_session_id,
                "server_ack_at": _iso_datetime(now_datetime()),
                "mobile_can_purge": True,
            }

        frappe.db.rollback()
        return _error(
            "ERPNext n’a pas pu créer la session d’inventaire. La session reste sur le téléphone.",
            log=True,
            error_code="SESSION_CREATE_FAILED",
            error_stage="session_insert_or_submit",
            campaign=campaign,
            inventory_agent=_safe_str((context.get("inventory_agent") or {}).get("name")) or _safe_str((context.get("inventory_agent") or {}).get("inventory_agent")),
            mobile_session_id=mobile_session_id,
            device_id=device_id,
            payload=sanitized_payload,
            technical_message=str(exc),
            traceback=frappe.get_traceback(),
            exception=str(exc),
            mobile_can_purge=False,
        )

    return {
        "ok": True,
        "submitted": True,
        "ack": True,
        "duplicate": False,
        "idempotent_ack": False,
        "inventory_session": doc.name,
        "status": doc.status,
        "campaign": campaign_doc.name,
        "inventory_agent": doc.inventory_agent,
        "mobile_session_id": mobile_session_id,
        "server_ack_at": _iso_datetime(doc.server_ack_at),
        "total_items_counted": total_items_counted,
        "total_qty_counted": total_qty_counted,
        "unplanned_items_count": len(unplanned_items),
        "unplanned_warehouses_count": len(unplanned_warehouses),
        "recoding_proposals_count": recoding_proposals_count,
        "mobile_can_purge": True,
        "next_step": "purge_mobile_local_data",
    }
