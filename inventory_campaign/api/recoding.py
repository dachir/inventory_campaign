# inventory_campaign/inventory_campaign/api/recoding.py

"""
Sprint 2.5 - Optional item recoding helpers.

The mobile app may scan a booklet QR code to propose a controlled recoding for
an inventory item. In the MVP, external inventory agents are allowed to propose
only:

- Famille
- Category

The QR payload uses a keyed-object format. The top-level key is the tag type:

    {"famille": {"code": "HYD", "description": "Hydraulique"}}
    {"category": {"code": "HYD-POMPES", "parent_code": "HYD", "description": "Pompes"}}

This module validates and normalizes those payloads. It does not create or
update Item, Warehouse, Item Group, or any other master data.
"""

from __future__ import annotations

import json
from typing import Any

import frappe

BOOKLET_QR_PROTOCOL = "inventory_campaign_recoding_tag_v1"
ALLOWED_EXTERNAL_TAG_TYPES = {"famille", "sous_famille", "plant_floor", "workstation"}
SUPPORTED_TAG_TYPES = {"famille", "sous_famille", "category", "plant", "plant_floor", "workstation", "machine", "part_type"}

TAG_LABELS = {
    "famille": "Famille",
    "sous_famille": "Sous Famille",
    "category": "Sous Famille (legacy alias)",
    "plant": "Plant",
    "plant_floor": "Plant",
    "workstation": "Machine",
    "machine": "Machine",
    "part_type": "Type de pièce",
}


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None



def _json_loads(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return value



def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))



def _one_root_key(data: dict[str, Any]) -> tuple[str | None, Any]:
    keys = [key for key in data.keys() if key not in {"protocol", "version"}]
    if len(keys) != 1:
        return None, None
    key = keys[0]
    return key, data.get(key)



def _normalize_allowed_tag_types(allowed_tag_types: Any = None) -> set[str]:
    if not allowed_tag_types:
        return set(ALLOWED_EXTERNAL_TAG_TYPES)

    if isinstance(allowed_tag_types, str):
        try:
            parsed = json.loads(allowed_tag_types)
        except Exception:
            parsed = [part.strip() for part in allowed_tag_types.split(",") if part.strip()]
    else:
        parsed = allowed_tag_types

    if isinstance(parsed, dict):
        parsed = parsed.get("allowed_tag_types") or parsed.get("tag_types") or []

    result = {_safe_str(tag) for tag in (parsed or [])}
    result = {tag for tag in result if tag}
    return result or set(ALLOWED_EXTERNAL_TAG_TYPES)


# -----------------------------------------------------------------------------
# Mobile / API contract
# -----------------------------------------------------------------------------


def get_mobile_recoding_config() -> dict[str, Any]:
    """Return the recoding rules that the mobile app should enforce."""

    return {
        "enabled": True,
        "optional": True,
        "booklet_qr_protocol": BOOKLET_QR_PROTOCOL,
        "qr_payload_format": "keyed_object",
        "freeze_required": True,
        "auto_apply_on_scan": False,
        "external_agent_allowed_tag_types": sorted(ALLOWED_EXTERNAL_TAG_TYPES),
        "supported_tag_types": sorted(SUPPORTED_TAG_TYPES),
        "official_nomenclature": {
            "famille": "Former Category / main family",
            "sous_famille": "Sous Famille",
        },
        "rules": {
            "external_agents_define_famille_sous_famille_caracteristiques_plant_machine": True,
            "plant_machine_are_usage_context_not_item_code": True,
            "mobile_updates_item_master": False,
            "mobile_creates_master_data": False,
            "supervisor_review_required_before_item_master_change": True,
        },
        "examples": {
            "famille": {"famille": {"code": "HYD", "description": "Hydraulique"}},
            "sous_famille": {"sous_famille": {"code": "PO", "parent_code": "HY", "description": "Pompes"}},
            "plant_floor": {"plant_floor": {"code": "PF-001", "description": "Plant Floor 001"}},
            "workstation": {"workstation": {"code": "WS-001", "parent_code": "PF-001", "description": "Machine 001"}},
        },
    }



def parse_booklet_qr_payload_value(
    payload: Any,
    allowed_tag_types: Any = None,
) -> dict[str, Any]:
    """
    Parse and validate a booklet QR payload.

    This is the internal value-based version used by both whitelisted APIs and
    future submit/session logic.
    """

    allowed = _normalize_allowed_tag_types(allowed_tag_types)

    try:
        data = _json_loads(payload)
    except Exception:
        return {
            "ok": False,
            "valid": False,
            "reason": "QR payload is not valid JSON.",
        }

    if not isinstance(data, dict):
        return {
            "ok": False,
            "valid": False,
            "reason": "QR payload must be a JSON object.",
        }

    tag_type, tag_value = _one_root_key(data)
    if not tag_type:
        return {
            "ok": False,
            "valid": False,
            "reason": "QR payload must contain exactly one recoding tag root key.",
        }

    tag_type = str(tag_type).strip()
    if tag_type == "plant":
        tag_type = "plant_floor"
    if tag_type == "machine":
        tag_type = "workstation"
    if tag_type not in SUPPORTED_TAG_TYPES:
        return {
            "ok": False,
            "valid": False,
            "reason": f"Unsupported recoding tag type: {tag_type}.",
            "tag_type": tag_type,
            "supported_tag_types": sorted(SUPPORTED_TAG_TYPES),
        }

    if tag_type not in allowed:
        return {
            "ok": False,
            "valid": False,
            "reason": f"This agent/profile is not allowed to freeze {tag_type} tags.",
            "tag_type": tag_type,
            "allowed_tag_types": sorted(allowed),
        }

    if not isinstance(tag_value, dict):
        return {
            "ok": False,
            "valid": False,
            "reason": f"{tag_type} tag value must be a JSON object.",
            "tag_type": tag_type,
        }

    code = _safe_str(tag_value.get("code"))
    description = _safe_str(tag_value.get("description"))
    parent_code = _safe_str(tag_value.get("parent_code"))

    if not code:
        return {
            "ok": False,
            "valid": False,
            "reason": f"{tag_type} tag is missing code.",
            "tag_type": tag_type,
        }

    if not description:
        return {
            "ok": False,
            "valid": False,
            "reason": f"{tag_type} tag is missing description.",
            "tag_type": tag_type,
        }

    normalized_tag = {
        "code": code,
        "description": description,
    }

    if parent_code:
        normalized_tag["parent_code"] = parent_code

    normalized_payload = {tag_type: normalized_tag}
    preview_label = f"{TAG_LABELS.get(tag_type, tag_type)} détecté: {code} — {description}"

    return {
        "ok": True,
        "valid": True,
        "tag_type": tag_type,
        "tag": normalized_tag,
        "normalized_payload": normalized_payload,
        "normalized_payload_json": _json_dumps(normalized_payload),
        "preview_label": preview_label,
        "freeze_required": True,
        "auto_apply_on_scan": False,
    }



def _normalize_tag_dict(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        return None

    code = _safe_str(value.get("code") or value.get("name"))
    description = _safe_str(value.get("description") or value.get("label"))
    parent_code = _safe_str(
        value.get("plant_floor")
        or value.get("plant_floor_code")
        or value.get("parent_code")
        or value.get("parent")
        or value.get("plant")
        or value.get("plantFloor")
    )

    if not code or not description:
        return None

    result = {
        "code": code,
        "description": description,
    }
    if parent_code:
        result["parent_code"] = parent_code
    return result


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "oui"}
    return False



def _doctype_record_exists(doctype: str, tag: dict[str, Any] | None) -> bool:
    """Validate that a selected ERPNext standard tag still exists.

    The mobile receives Plant Floor and Workstation from ERPNext. At submit time
    we still re-check the record to avoid accepting stale cached values. The
    check is intentionally tolerant: if the DocType is missing on a development
    site, validation is not blocked.
    """

    if not tag:
        return True
    if not frappe.db.exists("DocType", doctype):
        return True

    code = _safe_str(tag.get("code") or tag.get("name"))
    if not code:
        return False

    if frappe.db.exists(doctype, code):
        return True

    # Some deployments may expose a custom code field, while standard ERPNext
    # records usually use name. Check code only if the field exists.
    try:
        if frappe.get_meta(doctype).has_field("code"):
            return bool(frappe.db.exists(doctype, {"code": code}))
    except Exception:
        pass

    return False


def _has_field(doctype: str, fieldname: str) -> bool:
    try:
        return bool(frappe.get_meta(doctype).has_field(fieldname))
    except Exception:
        return False


def _resolve_doctype_name(doctype: str, code: str | None) -> str | None:
    code = _safe_str(code)
    if not code:
        return None

    if frappe.db.exists(doctype, code):
        return code

    try:
        if frappe.get_meta(doctype).has_field("code"):
            return frappe.db.get_value(doctype, {"code": code}, "name")
    except Exception:
        pass

    return None


def _get_workstation_plant_floor(workstation: dict[str, Any] | None) -> str | None:
    """Return the real Plant Floor linked on the ERPNext Workstation record."""

    if not workstation or not frappe.db.exists("DocType", "Workstation"):
        return None

    if not _has_field("Workstation", "plant_floor"):
        return None

    workstation_name = _resolve_doctype_name(
        "Workstation",
        _safe_str(workstation.get("code") or workstation.get("name")),
    )
    if not workstation_name:
        return None

    return _safe_str(frappe.db.get_value("Workstation", workstation_name, "plant_floor"))


def _normalize_characteristics(value: Any) -> list[dict[str, Any]]:
    """Normalize up to five characteristics without dropping non-major rows.

    Business rule:
    - recoding_tags_json must keep every characteristic captured on mobile;
    - exactly one row is marked as major for the dedicated reporting fields;
    - if the mobile explicitly sends is_major, the first truthy row wins;
    - if no row is marked major, the first valid characteristic becomes major.
    """

    rows = value if isinstance(value, list) else []
    prepared: list[dict[str, Any]] = []
    explicit_major_index: int | None = None

    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue

        property_code = _safe_str(
            raw_row.get("property_code")
            or raw_row.get("propriete")
            or raw_row.get("property")
            or raw_row.get("code")
        )
        description = _safe_str(
            raw_row.get("description")
            or raw_row.get("property_description")
            or raw_row.get("label")
            or property_code
        )
        value_text = _safe_str(raw_row.get("valeur") or raw_row.get("value"))
        unit = _safe_str(raw_row.get("unite") or raw_row.get("unit"))

        if not property_code or not value_text:
            continue

        if explicit_major_index is None and _is_truthy(raw_row.get("is_major")):
            explicit_major_index = len(prepared)

        prepared.append({
            "propriete": property_code,
            "property_code": property_code,
            "description": description or property_code,
            "valeur": value_text,
            "value": value_text,
            **({"unite": unit, "unit": unit} if unit else {}),
        })

        if len(prepared) >= 5:
            break

    if not prepared:
        return []

    major_index = explicit_major_index if explicit_major_index is not None else 0
    result: list[dict[str, Any]] = []

    for index, row in enumerate(prepared, start=1):
        normalized_row = {
            "sequence": index,
            **row,
            "is_major": 1 if (index - 1) == major_index else 0,
        }
        result.append(normalized_row)

    return result


def _major_characteristic_label(row: dict[str, Any] | None) -> str | None:
    """Return only the major property description for dedicated fields.

    The value/unit remains in recoding_tags_json.caracteristiques[]. The flat
    field recoding_caracteristique_majeure is only for filtering/reporting by
    the major characteristic itself.
    """

    if not row:
        return None
    return _safe_str(row.get("description")) or _safe_str(row.get("property_code"))


def normalize_recoding_tags_value(tags: Any) -> dict[str, Any]:
    """Normalize a complete recoding_tags_json value.

    Current mobile contract:
    - famille
    - sous_famille
    - caracteristiques
    - plant_floor
    - workstation

    ``caracteristiques`` always keeps all valid characteristics captured by the
    mobile app. Only the row marked ``is_major = 1`` is additionally exposed in
    the flat summary fields. The legacy key ``category`` may still be accepted as
    an input alias for old cached mobile payloads, but it is never emitted
    anymore. Category no longer exists in the functional model.
    """

    try:
        data = _json_loads(tags) or {}
    except Exception:
        return {
            "ok": False,
            "valid": False,
            "reason": "recoding_tags_json is not valid JSON.",
        }

    if not isinstance(data, dict):
        return {
            "ok": False,
            "valid": False,
            "reason": "recoding_tags_json must be a JSON object.",
        }

    famille = _normalize_tag_dict(data.get("famille") or data.get("family"))
    sous_famille = _normalize_tag_dict(
        data.get("sous_famille")
        or data.get("sousFamille")
        or data.get("sub_family")
        or data.get("category")  # legacy input alias only
    )
    caracteristiques = _normalize_characteristics(
        data.get("caracteristiques") or data.get("characteristics")
    )
    plant_floor = _normalize_tag_dict(
        data.get("plant_floor") or data.get("plantFloor") or data.get("plant") or data.get("usine")
    )
    workstation = _normalize_tag_dict(
        data.get("workstation") or data.get("machine")
    )

    if famille and sous_famille:
        parent_code = _safe_str(sous_famille.get("parent_code"))
        famille_code = _safe_str(famille.get("code"))
        if parent_code and famille_code and parent_code != famille_code:
            return {
                "ok": False,
                "valid": False,
                "reason": "Sous Famille parent_code conflicts with Famille code.",
                "famille_code": famille_code,
                "sous_famille_parent_code": parent_code,
            }

    if plant_floor and not _doctype_record_exists("Plant Floor", plant_floor):
        return {
            "ok": False,
            "valid": False,
            "reason": "Selected Plant Floor does not exist in ERPNext.",
            "plant_floor": plant_floor,
        }

    if workstation and not _doctype_record_exists("Workstation", workstation):
        return {
            "ok": False,
            "valid": False,
            "reason": "Selected Workstation does not exist in ERPNext.",
            "workstation": workstation,
        }

    if workstation and not plant_floor:
        return {
            "ok": False,
            "valid": False,
            "reason": "A Workstation cannot be submitted without its parent Plant Floor.",
            "workstation": workstation,
        }

    if plant_floor and workstation:
        plant_floor_code = _safe_str(plant_floor.get("code"))
        workstation_parent = _get_workstation_plant_floor(workstation)

        if not workstation_parent:
            return {
                "ok": False,
                "valid": False,
                "reason": "Selected Workstation is not linked to a Plant Floor in ERPNext.",
                "plant_floor_code": plant_floor_code,
                "workstation": workstation,
            }

        if plant_floor_code and workstation_parent != plant_floor_code:
            return {
                "ok": False,
                "valid": False,
                "reason": "Selected Workstation does not belong to the selected Plant Floor.",
                "plant_floor_code": plant_floor_code,
                "workstation_parent_code": workstation_parent,
            }

        # Trust the server-side ERPNext relationship, not the cached mobile value.
        workstation["parent_code"] = workstation_parent
        workstation["plant_floor"] = workstation_parent

    normalized: dict[str, Any] = {}
    if famille:
        normalized["famille"] = famille
    if sous_famille:
        normalized["sous_famille"] = sous_famille
    if caracteristiques:
        normalized["caracteristiques"] = caracteristiques
    if plant_floor:
        normalized["plant_floor"] = plant_floor
    if workstation:
        normalized["workstation"] = workstation

    major = next((row for row in caracteristiques if _safe_str(row.get("is_major")) == "1"), None)
    has_recoding = bool(normalized)

    return {
        "ok": True,
        "valid": True,
        "recoding_required": has_recoding,
        "recoding_status": "Pending Review" if has_recoding else "Not Required",
        "recoding_tags": normalized,
        "recoding_tags_json": _json_dumps(normalized),
        "summary": {
            "famille_code": _safe_str((famille or {}).get("code")),
            "famille_description": _safe_str((famille or {}).get("description")),
            "sous_famille_code": _safe_str((sous_famille or {}).get("code")),
            "sous_famille_description": _safe_str((sous_famille or {}).get("description")),
            "caracteristique_majeure_code": _safe_str((major or {}).get("property_code")),
            "caracteristique_majeure": _major_characteristic_label(major),
            "plant_floor": _safe_str((plant_floor or {}).get("code")),
            "plant_floor_description": _safe_str((plant_floor or {}).get("description")),
            "workstation": _safe_str((workstation or {}).get("code")),
            "workstation_description": _safe_str((workstation or {}).get("description")),
        },
    }



def apply_recoding_tag_value(
    current_tags: Any = None,
    qr_payload: Any = None,
    allowed_tag_types: Any = None,
) -> dict[str, Any]:
    """
    Apply a validated/frozen booklet QR tag to current recoding tags.

    This function represents the mobile UX rule: QR detected is only a preview;
    the tag becomes part of the recoding proposal only after the user presses
    the freeze/confirm button.
    """

    parsed = parse_booklet_qr_payload_value(qr_payload, allowed_tag_types=allowed_tag_types)
    if not parsed.get("valid"):
        return parsed

    try:
        current = _json_loads(current_tags) or {}
    except Exception:
        return {
            "ok": False,
            "valid": False,
            "reason": "Current recoding tags are not valid JSON.",
        }

    if not isinstance(current, dict):
        return {
            "ok": False,
            "valid": False,
            "reason": "Current recoding tags must be a JSON object.",
        }

    tag_type = parsed["tag_type"]
    tag = parsed["tag"]

    if tag_type == "plant":
        tag_type = "plant_floor"
    if tag_type == "machine":
        tag_type = "workstation"

    if tag_type == "category":
        existing_famille = current.get("famille") if isinstance(current.get("famille"), dict) else None
        parent_code = _safe_str(tag.get("parent_code"))
        existing_famille_code = _safe_str((existing_famille or {}).get("code"))
        if parent_code and existing_famille_code and parent_code != existing_famille_code:
            return {
                "ok": False,
                "valid": False,
                "reason": "Scanned Category does not belong to the already frozen Famille.",
                "existing_famille_code": existing_famille_code,
                "category_parent_code": parent_code,
            }

    if tag_type == "famille":
        existing_category = current.get("category") if isinstance(current.get("category"), dict) else None
        category_parent_code = _safe_str((existing_category or {}).get("parent_code"))
        new_famille_code = _safe_str(tag.get("code"))
        if category_parent_code and new_famille_code and category_parent_code != new_famille_code:
            return {
                "ok": False,
                "valid": False,
                "reason": "Scanned Famille conflicts with the already frozen Category parent_code.",
                "new_famille_code": new_famille_code,
                "category_parent_code": category_parent_code,
            }

    current[tag_type] = tag
    normalized = normalize_recoding_tags_value(current)
    if not normalized.get("valid"):
        return normalized

    return {
        "ok": True,
        "valid": True,
        "applied": True,
        "tag_type": tag_type,
        "tag": tag,
        **normalized,
    }


# -----------------------------------------------------------------------------
# Whitelisted APIs for testing/mobile integration
# -----------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def get_recoding_config() -> dict[str, Any]:
    return {
        "ok": True,
        "recoding": get_mobile_recoding_config(),
    }


@frappe.whitelist(allow_guest=True)
def parse_booklet_qr_payload(
    payload: Any = None,
    allowed_tag_types: Any = None,
) -> dict[str, Any]:
    return parse_booklet_qr_payload_value(payload, allowed_tag_types=allowed_tag_types)


@frappe.whitelist(allow_guest=True)
def apply_recoding_tag(
    current_tags_json: Any = None,
    qr_payload: Any = None,
    allowed_tag_types: Any = None,
) -> dict[str, Any]:
    return apply_recoding_tag_value(
        current_tags=current_tags_json,
        qr_payload=qr_payload,
        allowed_tag_types=allowed_tag_types,
    )


@frappe.whitelist(allow_guest=True)
def normalize_recoding_tags(tags_json: Any = None) -> dict[str, Any]:
    return normalize_recoding_tags_value(tags_json)
