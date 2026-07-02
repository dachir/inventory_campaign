# inventory_campaign/inventory_campaign/api/agent.py

"""
Sprint 2 - Inventory Agent token and mobile context APIs.

Architecture:
- The root credential is the one clear token stored on Inventory Agent.agent_token
  and validated through Inventory Agent.agent_token_hash.
- Inventory Access Token is legacy/dormant and is not used by this module.
- A successful root-token validation returns a short-lived signed mobile credential.
- The mobile credential authorizes context loading. It does not create an
  ERPNext Inventory Session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from io import BytesIO
from typing import Any

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime


MANAGER_ROLES = {"System Manager", "Stock Manager", "Inventory Campaign Manager"}
AGENT_TOKEN_QR_PROTOCOL = "inventory_campaign_agent_token_v1"
MOBILE_CREDENTIAL_PROTOCOL = "inventory_campaign_mobile_credential_v1"
DEFAULT_ROOT_TOKEN_VALID_DAYS = 30
DEFAULT_MOBILE_CREDENTIAL_TTL_MINUTES = 480


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



def _safe_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default



def _doc_get(doc: Any, fieldname: str, default: Any = None) -> Any:
    if not doc:
        return default
    try:
        return doc.get(fieldname, default)
    except Exception:
        return getattr(doc, fieldname, default)


def _doc_has_field(doc: Any, fieldname: str) -> bool:
    if not doc or not fieldname:
        return False
    doctype = _safe_str(_doc_get(doc, "doctype"))
    if doctype and _has_field(doctype, fieldname):
        return True
    try:
        return bool(doc.meta.has_field(fieldname))
    except Exception:
        return False


def _doc_set_if_field(doc: Any, fieldname: str, value: Any) -> None:
    if _doc_has_field(doc, fieldname):
        doc.set(fieldname, value)



def _iso_datetime(value: Any) -> str | None:
    if not value:
        return None
    try:
        return get_datetime(value).isoformat()
    except Exception:
        return str(value)



def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))



def _base64_urlsafe_encode(raw_bytes: bytes) -> str:
    return base64.urlsafe_b64encode(raw_bytes).decode("utf-8").rstrip("=")



def _base64_urlsafe_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("utf-8"))



def _get_server_secret() -> bytes:
    secret = frappe.conf.get("encryption_key") or frappe.conf.get("secret_key")
    if not secret:
        secret = getattr(frappe.local, "site", None) or "inventory-campaign-local-secret"
    return str(secret).encode("utf-8")



def _has_doctype(doctype: str) -> bool:
    return bool(frappe.db.exists("DocType", doctype))



def _has_field(doctype: str, fieldname: str) -> bool:
    if not _has_doctype(doctype):
        return False
    try:
        return bool(frappe.get_meta(doctype).has_field(fieldname))
    except Exception:
        return False



def _require_manager_role() -> None:
    if frappe.session.user == "Guest":
        frappe.throw("Authentication required.", frappe.PermissionError)

    user_roles = set(frappe.get_roles(frappe.session.user))
    if not (user_roles & MANAGER_ROLES):
        frappe.throw(
            "Only System Manager, Stock Manager, or Inventory Campaign Manager can manage Inventory Agent tokens.",
            frappe.PermissionError,
        )



def _get_settings_value(fieldname: str, default: Any = None) -> Any:
    if not _has_doctype("Inventory Campaign Settings"):
        return default
    if not _has_field("Inventory Campaign Settings", fieldname):
        return default
    try:
        value = frappe.db.get_single_value("Inventory Campaign Settings", fieldname)
        if value in (None, ""):
            return default
        return value
    except Exception:
        return default


def _normalize_url_without_trailing_slash(value: Any) -> str | None:
    value = _safe_str(value)
    if not value:
        return None
    return value.rstrip("/")


def _normalize_protocol(value: Any, default: str = "http") -> str:
    protocol = (_safe_str(value) or default).replace("://", "").replace("/", "").lower()
    return protocol if protocol in {"http", "https"} else default


def _strip_protocol_and_slashes(value: Any) -> str | None:
    value = _safe_str(value)
    if not value:
        return None
    value = value.replace("https://", "", 1).replace("http://", "", 1)
    return value.strip().strip("/") or None


def _get_server_url() -> str | None:
    """Return Inventory Campaign Settings.server_reachable_url for mobile use.

    Priority:
    1. Inventory Campaign Settings.server_reachable_url
    2. protocol/protocole + server_url
    3. frappe.utils.get_url() fallback

    The mobile app should persist this value as its base URL after activation.
    """

    configured_url = _normalize_url_without_trailing_slash(
        _get_settings_value("server_reachable_url")
    )
    if configured_url:
        if configured_url.startswith(("http://", "https://")):
            return configured_url
        protocol = _normalize_protocol(
            _get_settings_value("protocol") or _get_settings_value("protocole"),
            default="http",
        )
        return f"{protocol}://{_strip_protocol_and_slashes(configured_url)}"

    protocol = _normalize_protocol(
        _get_settings_value("protocol") or _get_settings_value("protocole"),
        default="http",
    )
    host = _strip_protocol_and_slashes(_get_settings_value("server_url"))
    if host:
        return f"{protocol}://{host}"

    try:
        return frappe.utils.get_url().rstrip("/")
    except Exception:
        site = _safe_str(getattr(frappe.local, "site", None))
        return f"{protocol}://{site}" if site else None



def _get_security_context() -> dict[str, Any]:
    try:
        from inventory_campaign.api.security import get_security_context_dict

        return get_security_context_dict()
    except Exception:
        return {
            "ok": True,
            "settings_available": False,
            "security_mode": "Disabled",
            "security_blocking": False,
            "security_audit_only": False,
            "security_disabled": True,
            "require_network_check": False,
            "effective_require_network_check": False,
            "audit_network_check": False,
            "log_security_events": False,
        }


def _get_recoding_mobile_config() -> dict[str, Any]:
    try:
        from inventory_campaign.api.recoding import get_mobile_recoding_config

        return get_mobile_recoding_config()
    except Exception:
        return {
            "enabled": True,
            "optional": True,
            "qr_payload_format": "keyed_object",
            "freeze_required": True,
            "auto_apply_on_scan": False,
            "external_agent_allowed_tag_types": ["category", "famille"],
            "rules": {
                "external_agents_define_only_famille_and_category": True,
                "mobile_updates_item_master": False,
                "mobile_creates_master_data": False,
            },
        }



def _log_security_event(**kwargs: Any) -> str | None:
    try:
        from inventory_campaign.api.security import log_security_event

        return log_security_event(**kwargs)
    except Exception:
        return None



def _validate_network(ip_address: str | None = None, ssid: str | None = None) -> dict[str, Any]:
    try:
        from inventory_campaign.api.security import _validate_network_values

        return _validate_network_values(ip_address=ip_address, ssid=ssid)
    except Exception:
        frappe.log_error(
            title="Inventory Campaign - agent_network_validation_failed",
            message=frappe.get_traceback(),
        )
        return {
            "valid": False,
            "reason": "Network validation failed because of a server error",
            "event_type": "NETWORK_VALIDATION_ERROR",
            "ip_address": ip_address,
            "ssid": ssid,
        }


# -----------------------------------------------------------------------------
# Root Inventory Agent token helpers
# -----------------------------------------------------------------------------


def hash_agent_token(token: str | None) -> str | None:
    token = _safe_str(token)
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()



def _generate_clear_agent_token() -> str:
    # 32 bytes gives a high-entropy URL-safe root token.
    return secrets.token_urlsafe(32)



def _find_agent_by_token(token: str | None) -> Any | None:
    token_hash = hash_agent_token(token)
    if not token_hash:
        return None

    agent_name = frappe.db.get_value(
        "Inventory Agent",
        {"agent_token_hash": token_hash},
        "name",
    )

    if not agent_name:
        return None

    return frappe.get_doc("Inventory Agent", agent_name)



def _get_clear_agent_token(agent_doc: Any) -> str | None:
    if not agent_doc:
        return None

    try:
        return _safe_str(agent_doc.get_password("agent_token", raise_exception=False))
    except Exception:
        return _safe_str(_doc_get(agent_doc, "agent_token"))



def _set_agent_password(agent_doc: Any, clear_token: str | None) -> None:
    try:
        agent_doc.set_password("agent_token", clear_token or "")
    except Exception:
        agent_doc.set("agent_token", clear_token or "")



def _date_is_within(valid_from: Any = None, valid_until: Any = None) -> bool:
    now_value = now_datetime()
    from_value = get_datetime(valid_from) if valid_from else None
    until_value = get_datetime(valid_until) if valid_until else None

    if from_value and now_value < from_value:
        return False
    if until_value and now_value > until_value:
        return False
    return True



def _get_mobile_credential_ttl(agent_doc: Any) -> int:
    ttl = _safe_int(_doc_get(agent_doc, "mobile_credential_ttl_minutes"), 0)
    if ttl > 0:
        return ttl

    return _safe_int(
        _get_settings_value("default_mobile_credential_ttl_minutes", DEFAULT_MOBILE_CREDENTIAL_TTL_MINUTES),
        DEFAULT_MOBILE_CREDENTIAL_TTL_MINUTES,
    )



def _build_qr_payload(agent_doc: Any, clear_token: str) -> dict[str, Any]:
    """Build the final minimal mobile access QR payload.

    Keep the QR as small and stable as possible. The mobile needs only the
    reachable ERPNext URL and the clear one-time agent token. After scanning,
    the mobile calls validate_agent_access_token() and ERPNext returns the full
    context: agent, company, campaigns, codification referential,
    mobile_credential, etc.
    """

    server_url = _get_server_url()
    if not server_url:
        frappe.throw(
            "Inventory Campaign Settings.server_reachable_url is required to generate the mobile QR."
        )

    return {
        "server_reachable_url": server_url,
        "agent_token": clear_token,
    }


def _make_qr_png_data_url(payload: dict[str, Any]) -> str | None:
    """
    Generate a QR PNG data URL when the optional qrcode package is available.

    Frappe/ERPNext installations often already carry the dependency through
    payments/fiscal integrations, but the API must remain usable without it.
    """

    try:
        import qrcode  # type: ignore

        image = qrcode.make(_json_dumps(payload))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return None


@frappe.whitelist()
def generate_agent_token(
    inventory_agent: str,
    valid_days: int | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
) -> dict[str, Any]:
    """
    Generate/regenerate the one clear root token of an Inventory Agent.

    The admin can either provide an explicit validity window
    (valid_from + valid_until) or let the API use valid_days from now.
    The clear token is returned in the response so ERPNext can immediately
    render a QR for the mobile app.
    """

    _require_manager_role()

    inventory_agent = _safe_str(inventory_agent)
    if not inventory_agent or not frappe.db.exists("Inventory Agent", inventory_agent):
        frappe.throw("Inventory Agent does not exist.")

    agent_doc = frappe.get_doc("Inventory Agent", inventory_agent)
    clear_token = _generate_clear_agent_token()
    token_hash = hash_agent_token(clear_token)

    if valid_from:
        valid_from_value = get_datetime(valid_from)
    else:
        valid_from_value = now_datetime()

    if valid_until:
        valid_until_value = get_datetime(valid_until)
    else:
        valid_days = _safe_int(valid_days, DEFAULT_ROOT_TOKEN_VALID_DAYS)
        if valid_days <= 0:
            valid_days = DEFAULT_ROOT_TOKEN_VALID_DAYS
        valid_until_value = add_to_date(valid_from_value, days=valid_days)

    if valid_until_value <= valid_from_value:
        frappe.throw("Token end date must be after token start date.")

    _set_agent_password(agent_doc, clear_token)
    agent_doc.agent_token_hash = token_hash
    agent_doc.token_status = "Active"
    agent_doc.token_valid_from = valid_from_value
    agent_doc.token_valid_until = valid_until_value
    agent_doc.last_token_used_at = None
    _doc_set_if_field(agent_doc, "token_consumed_at", None)
    _doc_set_if_field(agent_doc, "token_consumed_by_device", None)
    _doc_set_if_field(agent_doc, "credential_issued_at", None)
    _doc_set_if_field(agent_doc, "credential_expires_at", None)

    agent_doc.save(ignore_permissions=True)
    frappe.db.commit()

    qr_payload = _build_qr_payload(agent_doc, clear_token)
    qr_png_data_url = _make_qr_png_data_url(qr_payload)

    return {
        "ok": True,
        "inventory_agent": agent_doc.name,
        "agent_code": _doc_get(agent_doc, "agent_code"),
        "agent_name": _doc_get(agent_doc, "agent_name"),
        "token": clear_token,
        "token_status": "Active",
        "token_valid_from": _iso_datetime(valid_from_value),
        "token_valid_until": _iso_datetime(valid_until_value),
        "qr_payload": qr_payload,
        "qr_payload_json": _json_dumps(qr_payload),
        "qr_png_data_url": qr_png_data_url,
        "qr_generation_available": bool(qr_png_data_url),
        "warning": None if qr_png_data_url else "QR PNG generation requires the optional Python package qrcode. The mobile can still scan/use qr_payload_json or the clear token.",
    }


@frappe.whitelist()
def get_agent_token_qr_payload(inventory_agent: str) -> dict[str, Any]:
    """
    Return a QR payload for the current active Inventory Agent token.

    This exposes the clear root token, so it is manager-only.
    """

    _require_manager_role()

    inventory_agent = _safe_str(inventory_agent)
    if not inventory_agent or not frappe.db.exists("Inventory Agent", inventory_agent):
        frappe.throw("Inventory Agent does not exist.")

    agent_doc = frappe.get_doc("Inventory Agent", inventory_agent)

    if _doc_get(agent_doc, "token_status") != "Active":
        frappe.throw("No active token is available for this Inventory Agent. Generate a new token first.")

    clear_token = _get_clear_agent_token(agent_doc)

    if not clear_token or not _doc_get(agent_doc, "agent_token_hash"):
        frappe.throw("No active token is stored for this Inventory Agent. Generate a token first.")

    qr_payload = _build_qr_payload(agent_doc, clear_token)
    qr_png_data_url = _make_qr_png_data_url(qr_payload)

    return {
        "ok": True,
        "inventory_agent": agent_doc.name,
        "token_status": _doc_get(agent_doc, "token_status"),
        "token_valid_from": _iso_datetime(_doc_get(agent_doc, "token_valid_from")),
        "token_valid_until": _iso_datetime(_doc_get(agent_doc, "token_valid_until")),
        "qr_payload": qr_payload,
        "qr_payload_json": _json_dumps(qr_payload),
        "qr_png_data_url": qr_png_data_url,
        "qr_generation_available": bool(qr_png_data_url),
    }


@frappe.whitelist()
def disable_agent_token(inventory_agent: str) -> dict[str, Any]:
    _require_manager_role()

    inventory_agent = _safe_str(inventory_agent)
    if not inventory_agent or not frappe.db.exists("Inventory Agent", inventory_agent):
        frappe.throw("Inventory Agent does not exist.")

    agent_doc = frappe.get_doc("Inventory Agent", inventory_agent)
    _set_agent_password(agent_doc, None)
    agent_doc.agent_token_hash = None
    agent_doc.token_status = "Disabled"
    agent_doc.last_token_used_at = None
    agent_doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "ok": True,
        "inventory_agent": agent_doc.name,
        "token_status": "Disabled",
    }


@frappe.whitelist()
def reset_agent_device_binding(inventory_agent: str) -> dict[str, Any]:
    _require_manager_role()

    inventory_agent = _safe_str(inventory_agent)
    if not inventory_agent or not frappe.db.exists("Inventory Agent", inventory_agent):
        frappe.throw("Inventory Agent does not exist.")

    agent_doc = frappe.get_doc("Inventory Agent", inventory_agent)
    agent_doc.bound_device_id = None
    agent_doc.bound_at = None
    agent_doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "ok": True,
        "inventory_agent": agent_doc.name,
        "bound_device_id": None,
        "bound_at": None,
    }


# -----------------------------------------------------------------------------
# Context builders
# -----------------------------------------------------------------------------


def _warehouse_name(warehouse: str | None) -> str | None:
    warehouse = _safe_str(warehouse)
    if not warehouse:
        return None
    return frappe.db.get_value("Warehouse", warehouse, "warehouse_name")



def _warehouse_parent(warehouse: str | None) -> str | None:
    warehouse = _safe_str(warehouse)
    if not warehouse:
        return None
    return frappe.db.get_value("Warehouse", warehouse, "parent_warehouse")



def _get_agent_context(agent_doc: Any) -> dict[str, Any]:
    return {
        "name": agent_doc.name,
        "agent_code": _doc_get(agent_doc, "agent_code"),
        "agent_name": _doc_get(agent_doc, "agent_name"),
        "status": _doc_get(agent_doc, "status"),
        "phone": _doc_get(agent_doc, "phone"),
        "email": _doc_get(agent_doc, "email"),
        "company": _doc_get(agent_doc, "company"),
    }


def _get_branch_label(branch_name: str | None) -> str | None:
    branch_name = _safe_str(branch_name)
    if not branch_name or not _has_doctype("Branch"):
        return branch_name

    fields = ["name"]
    for optional in ("branch", "branch_name"):
        if _has_field("Branch", optional):
            fields.append(optional)

    try:
        row = frappe.db.get_value("Branch", branch_name, fields, as_dict=True)
    except Exception:
        return branch_name

    if not row:
        return branch_name

    return _safe_str(row.get("branch_name")) or _safe_str(row.get("branch")) or _safe_str(row.get("name"))


def _get_all_branches(agent_doc: Any | None = None) -> list[dict[str, Any]]:
    """Return all ERPNext Branch records for the mobile site selector.

    Functional rule: the mobile app must receive the complete ERPNext Branch
    list using the ERPNext source of truth.  Keep this deliberately simple:

        SELECT name FROM `tabBranch`

    Branch is the business site selected by the agent when opening a local
    counting session. It is not the technical ERPNext URL/site.
    """

    if not _has_doctype("Branch"):
        return []

    try:
        rows = frappe.db.sql(
            """
            SELECT name
            FROM `tabBranch`
            ORDER BY name ASC
            """,
            as_dict=True,
        )
    except Exception:
        frappe.log_error(
            title="Inventory Campaign - mobile_branches_sql_failed",
            message=frappe.get_traceback(),
        )
        return []

    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in rows:
        name = _safe_str(row.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        result.append({
            "name": name,
            "code": name,
            "branch": name,
            "branch_name": name,
            "label": name,
            "site": name,
            "site_name": name,
        })

    return result


def _get_item_barcodes(item_code: str) -> list[dict[str, Any]]:
    if not _has_doctype("Item Barcode"):
        return []

    try:
        return frappe.get_all(
            "Item Barcode",
            filters={"parent": item_code, "parenttype": "Item"},
            fields=["barcode", "uom"],
            order_by="idx asc",
        )
    except Exception:
        return []



def _get_item_context(item_code: str) -> dict[str, Any]:
    item_code = _safe_str(item_code)
    if not item_code:
        return {}

    item = frappe.db.get_value(
        "Item",
        item_code,
        ["name", "item_code", "item_name", "item_group", "stock_uom", "disabled"],
        as_dict=True,
    )

    if not item:
        return {
            "item_code": item_code,
            "exists": False,
        }

    return {
        "item_code": item.get("item_code") or item.get("name"),
        "item_name": item.get("item_name"),
        "item_group": item.get("item_group"),
        "stock_uom": item.get("stock_uom"),
        "disabled": cint(item.get("disabled")),
        "exists": True,
        "barcodes": _get_item_barcodes(item.get("name") or item_code),
    }





def _split_possible_values(raw: Any) -> list[str]:
    text = _safe_str(raw)
    if not text:
        return []

    values: list[str] = []
    for line in text.replace(";", "\n").splitlines():
        for part in line.split(","):
            value = _safe_str(part)
            if value and value not in values:
                values.append(value)
    return values


def _get_famille_context(famille: str | None) -> dict[str, Any] | None:
    famille = _safe_str(famille)
    if not famille or not _has_doctype("Famille") or not frappe.db.exists("Famille", famille):
        return None

    row = frappe.db.get_value("Famille", famille, ["name", "code", "description"], as_dict=True)
    if not row:
        return None
    code = row.get("code") or row.get("name")
    return {
        "name": row.get("name"),
        "code": code,
        "description": row.get("description") or code,
    }


def _get_sous_famille_context(sous_famille: str | None) -> dict[str, Any] | None:
    sous_famille = _safe_str(sous_famille)
    if not sous_famille or not _has_doctype("Sous Famille") or not frappe.db.exists("Sous Famille", sous_famille):
        return None

    row = frappe.db.get_value(
        "Sous Famille",
        sous_famille,
        ["name", "code", "description", "famille"],
        as_dict=True,
    )
    if not row:
        return None
    code = row.get("code") or row.get("name")
    return {
        "name": row.get("name"),
        "code": code,
        "description": row.get("description") or code,
        "parent_code": row.get("famille"),
        "famille": row.get("famille"),
    }


def _get_propriete_context(propriete: str | None) -> dict[str, Any] | None:
    propriete = _safe_str(propriete)
    if not propriete or not _has_doctype("Propriete") or not frappe.db.exists("Propriete", propriete):
        return None

    fields = ["name", "code", "description"]
    for optional in ["unite", "value_type", "valeurs_possibles"]:
        if _has_field("Propriete", optional):
            fields.append(optional)

    row = frappe.db.get_value("Propriete", propriete, fields, as_dict=True)
    if not row:
        return None

    code = row.get("code") or row.get("name")
    possible_values = _split_possible_values(row.get("valeurs_possibles"))
    return {
        "name": row.get("name"),
        "code": code,
        "description": row.get("description") or code,
        "default_unit": row.get("unite"),
        "unit": row.get("unite"),
        "value_type": row.get("value_type") or "Text",
        "possible_values": possible_values,
        "valeurs_possibles": possible_values,
    }


def _get_item_codification_context(item_code: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "famille": None,
        "sous_famille": None,
        "caracteristiques": [],
        "is_codified": False,
    }

    item_code = _safe_str(item_code)
    if not item_code or not frappe.db.exists("Item", item_code):
        return result

    try:
        item_doc = frappe.get_doc("Item", item_code)
    except Exception:
        return result

    famille = _safe_str(_doc_get(item_doc, "custom_famille"))
    sous_famille = _safe_str(_doc_get(item_doc, "custom_sous_famille"))

    result["famille"] = _get_famille_context(famille)
    result["sous_famille"] = _get_sous_famille_context(sous_famille)

    caracteristiques: list[dict[str, Any]] = []
    if _doc_has_field(item_doc, "custom_caracteristiques"):
        for row in item_doc.get("custom_caracteristiques") or []:
            propriete = _safe_str(_doc_get(row, "propriete"))
            prop_ctx = _get_propriete_context(propriete)
            sequence = _safe_int(_doc_get(row, "sequence"), len(caracteristiques) + 1)
            value = _safe_str(_doc_get(row, "valeur"))
            unit = _safe_str(_doc_get(row, "unite")) or (prop_ctx or {}).get("default_unit")
            property_code = (prop_ctx or {}).get("code") or propriete
            caracteristiques.append({
                "sequence": sequence,
                "propriete": propriete,
                "property_code": property_code,
                "property_name": propriete,
                "property_description": (prop_ctx or {}).get("description") or property_code,
                "description": (prop_ctx or {}).get("description") or property_code,
                "valeur": value,
                "value": value,
                "unite": unit,
                "unit": unit,
                "is_major": 1 if cint(_doc_get(row, "is_major", 0)) else 0,
            })

    caracteristiques.sort(key=lambda row: _safe_int(row.get("sequence"), 0))
    result["caracteristiques"] = caracteristiques
    result["is_codified"] = bool(result.get("famille") or result.get("sous_famille") or caracteristiques)
    return result


def _get_latest_modified(doctype: str) -> str | None:
    if not _has_doctype(doctype):
        return None
    try:
        row = frappe.get_all(doctype, fields=["modified"], order_by="modified desc", limit=1)
        if row:
            return _iso_datetime(row[0].get("modified"))
    except Exception:
        return None
    return None



def _get_existing_fields(doctype: str, candidates: list[str]) -> list[str]:
    if not _has_doctype(doctype):
        return []
    return [fieldname for fieldname in candidates if _has_field(doctype, fieldname)]


def _first_non_empty(row: dict[str, Any], candidates: list[str]) -> str | None:
    for fieldname in candidates:
        value = _safe_str(row.get(fieldname))
        if value:
            return value
    return None


def _get_plant_machine_referential() -> dict[str, Any]:
    """Return standard ERPNext Plant Floor / Workstation lists for mobile tags.

    These are not part of the spare-part code. They are usage/context tags that
    allow the mobile agent to indicate where the part is used.
    """

    plant_floors: list[dict[str, Any]] = []
    workstations: list[dict[str, Any]] = []

    if _has_doctype("Plant Floor"):
        plant_label_fields = _get_existing_fields(
            "Plant Floor",
            ["plant_floor_name", "title", "description"],
        )
        plant_fields = ["name", *plant_label_fields]
        for row in frappe.get_all("Plant Floor", fields=plant_fields, order_by="name asc"):
            name = row.get("name")
            label = _first_non_empty(row, plant_label_fields) or name
            plant_floors.append({
                "name": name,
                "code": name,
                "description": label,
                "label": label,
            })

    if _has_doctype("Workstation"):
        workstation_label_fields = _get_existing_fields(
            "Workstation",
            ["workstation_name", "title", "description"],
        )
        workstation_parent_fields = _get_existing_fields(
            "Workstation",
            ["plant_floor"],
        )
        workstation_flag_fields = _get_existing_fields(
            "Workstation",
            ["disabled", "is_disabled"],
        )
        workstation_fields = [
            "name",
            *workstation_label_fields,
            *workstation_parent_fields,
            *workstation_flag_fields,
        ]
        for row in frappe.get_all("Workstation", fields=workstation_fields, order_by="name asc"):
            if any(cint(row.get(fieldname)) for fieldname in workstation_flag_fields):
                continue
            name = row.get("name")
            label = _first_non_empty(row, workstation_label_fields) or name
            plant_floor = _first_non_empty(row, workstation_parent_fields)
            workstations.append({
                "name": name,
                "code": name,
                "description": label,
                "label": label,
                "parent_code": plant_floor,
                "plant_floor": plant_floor,
            })

    version_parts = [
        _get_latest_modified("Plant Floor"),
        _get_latest_modified("Workstation"),
    ]
    version = max([part for part in version_parts if part] or [now_datetime().isoformat()])
    data = {
        "plant_floors": plant_floors,
        "plants": plant_floors,
        "workstations": workstations,
        "machines": workstations,
        "version": version,
    }
    data["hash"] = hashlib.sha256(_json_dumps({
        "plant_floors": plant_floors,
        "workstations": workstations,
    }).encode("utf-8")).hexdigest()
    return data


def _get_codification_referential() -> dict[str, Any]:
    families: list[dict[str, Any]] = []
    sub_families: list[dict[str, Any]] = []
    properties: list[dict[str, Any]] = []
    sub_family_properties: list[dict[str, Any]] = []

    if _has_doctype("Famille"):
        for row in frappe.get_all("Famille", fields=["name", "code", "description"], order_by="code asc"):
            code = row.get("code") or row.get("name")
            families.append({
                "name": row.get("name"),
                "code": code,
                "description": row.get("description") or code,
            })

    if _has_doctype("Sous Famille"):
        for row in frappe.get_all("Sous Famille", fields=["name", "code", "description", "famille"], order_by="famille asc, code asc"):
            code = row.get("code") or row.get("name")
            sub_families.append({
                "name": row.get("name"),
                "code": code,
                "description": row.get("description") or code,
                "parent_code": row.get("famille"),
                "famille": row.get("famille"),
            })

    if _has_doctype("Propriete"):
        prop_fields = ["name", "code", "description"]
        for optional in ["unite", "value_type", "valeurs_possibles"]:
            if _has_field("Propriete", optional):
                prop_fields.append(optional)
        for row in frappe.get_all("Propriete", fields=prop_fields, order_by="code asc"):
            code = row.get("code") or row.get("name")
            possible_values = _split_possible_values(row.get("valeurs_possibles"))
            properties.append({
                "name": row.get("name"),
                "code": code,
                "description": row.get("description") or code,
                "default_unit": row.get("unite"),
                "unit": row.get("unite"),
                "value_type": row.get("value_type") or "Text",
                "possible_values": possible_values,
                "valeurs_possibles": possible_values,
            })

    if _has_doctype("Sous Famille Propriete"):
        rule_fields = ["name", "sous_famille", "famille", "propriete", "sequence"]
        for optional in ["is_major_default", "is_required", "raw_unit", "resolved_uom", "valeurs_possibles", "statut"]:
            if _has_field("Sous Famille Propriete", optional):
                rule_fields.append(optional)
        for row in frappe.get_all("Sous Famille Propriete", fields=rule_fields, order_by="sous_famille asc, sequence asc"):
            prop_ctx = _get_propriete_context(row.get("propriete")) or {}
            possible_values = _split_possible_values(row.get("valeurs_possibles")) or prop_ctx.get("possible_values") or []
            unit = row.get("resolved_uom") or row.get("raw_unit") or prop_ctx.get("default_unit")
            sub_family_properties.append({
                "name": row.get("name"),
                "sub_family_code": row.get("sous_famille"),
                "sous_famille": row.get("sous_famille"),
                "family_code": row.get("famille"),
                "famille": row.get("famille"),
                "property_code": row.get("propriete"),
                "propriete": row.get("propriete"),
                "property_description": prop_ctx.get("description") or row.get("propriete"),
                "sequence": _safe_int(row.get("sequence"), 0),
                "is_default_major": 1 if cint(row.get("is_major_default")) else 0,
                "is_major_default": 1 if cint(row.get("is_major_default")) else 0,
                "required": 1 if cint(row.get("is_required")) else 0,
                "is_required": 1 if cint(row.get("is_required")) else 0,
                "default_unit": unit,
                "unit": unit,
                "value_type": prop_ctx.get("value_type") or "Text",
                "possible_values": possible_values,
                "valeurs_possibles": possible_values,
                "statut": row.get("statut"),
            })

    plant_machine_referential = _get_plant_machine_referential()
    plant_floors = plant_machine_referential.get("plant_floors") or []
    workstations = plant_machine_referential.get("workstations") or []

    version_parts = [
        _get_latest_modified("Famille"),
        _get_latest_modified("Sous Famille"),
        _get_latest_modified("Propriete"),
        _get_latest_modified("Sous Famille Propriete"),
        plant_machine_referential.get("version"),
    ]
    version = max([part for part in version_parts if part] or [now_datetime().isoformat()])

    data = {
        "families": families,
        "familles": families,
        "sub_families": sub_families,
        "sous_familles": sub_families,
        "properties": properties,
        "proprietes": properties,
        "sub_family_properties": sub_family_properties,
        "sous_famille_proprietes": sub_family_properties,
        "plant_floors": plant_floors,
        "plants": plant_floors,
        "workstations": workstations,
        "machines": workstations,
        "version": version,
    }
    data["hash"] = hashlib.sha256(_json_dumps({
        "families": families,
        "sub_families": sub_families,
        "properties": properties,
        "sub_family_properties": sub_family_properties,
        "plant_floors": plant_floors,
        "workstations": workstations,
    }).encode("utf-8")).hexdigest()
    return data

def _get_authorized_locations(agent_doc: Any) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None]] = set()

    for row in agent_doc.get("authorized_locations") or []:
        if not cint(_doc_get(row, "active", 1)):
            continue

        location_warehouse = _safe_str(_doc_get(row, "location_warehouse"))
        parent_warehouse = _safe_str(_doc_get(row, "parent_warehouse")) or _warehouse_parent(location_warehouse)

        if not location_warehouse:
            continue

        key = (parent_warehouse, location_warehouse)
        if key in seen:
            continue
        seen.add(key)

        locations.append({
            "parent_warehouse": parent_warehouse,
            "parent_warehouse_name": _warehouse_name(parent_warehouse),
            "location_warehouse": location_warehouse,
            "location_name": _warehouse_name(location_warehouse),
            "active": 1,
        })

    return locations



def _get_authorized_item_groups(agent_doc: Any) -> list[dict[str, Any]]:
    """Return Item Groups selected on the Inventory Agent.

    The mobile scope is now driven by Item Groups only. The older
    authorized_items table is kept in the DocType as hidden legacy metadata, but
    it is not used to build the mobile item cache.
    """

    groups: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in agent_doc.get("authorized_item_groups") or []:
        if not cint(_doc_get(row, "active", 1)):
            continue

        item_group = _safe_str(_doc_get(row, "item_group"))
        if not item_group or item_group in seen:
            continue
        seen.add(item_group)

        groups.append({
            "item_group": item_group,
            "item_group_name": item_group,
            "active": 1,
        })

    return groups


def _get_exact_authorized_item_group_names(item_groups: list[str]) -> list[str]:
    """Return exactly the Item Groups selected on the Inventory Agent.

    This intentionally does not include child Item Groups. In this project,
    the agent scope is an explicit list of Item Groups, and only Items whose
    Item.item_group is directly in that list are downloaded to the mobile app.
    """

    result: list[str] = []
    seen: set[str] = set()

    for group in item_groups or []:
        group = _safe_str(group)
        if not group or group in seen:
            continue
        if _has_doctype("Item Group") and not frappe.db.exists("Item Group", group):
            continue
        seen.add(group)
        result.append(group)

    return result


def _get_authorized_item_group_names(agent_doc: Any) -> list[str]:
    selected_groups = _get_authorized_item_groups(agent_doc)
    return _get_exact_authorized_item_group_names([
        row.get("item_group")
        for row in selected_groups
        if _safe_str(row.get("item_group"))
    ])


def _count_authorized_items(agent_doc: Any) -> int:
    """Count active stock Items in the selected Item Groups without loading them."""

    authorized_group_names = _get_authorized_item_group_names(agent_doc)
    if not authorized_group_names:
        return 0

    filters: dict[str, Any] = {
        "disabled": 0,
        "item_group": ["in", authorized_group_names],
    }
    if _has_field("Item", "is_stock_item"):
        filters["is_stock_item"] = 1

    try:
        return cint(frappe.db.count("Item", filters=filters))
    except Exception:
        frappe.log_error(
            title="Inventory Campaign - authorized_item_group_count_failed",
            message=frappe.get_traceback(),
        )
        return 0


def _get_authorized_items(agent_doc: Any) -> list[dict[str, Any]]:
    """Deprecated: do not download the ERPNext Item catalog to mobile.

    With 16k+ Items, the mobile validates each scanned barcode online through
    validate_scanned_item. This function intentionally returns an empty list to
    keep validate_agent_access_token and get_inventory_context lightweight.
    """

    return []


def _item_is_authorized_for_agent(agent_doc: Any, item_group: str | None) -> bool:
    item_group = _safe_str(item_group)
    if not item_group:
        return False
    return item_group in set(_get_authorized_item_group_names(agent_doc))



def _location_parent_set(authorized_locations: list[dict[str, Any]]) -> set[str]:
    parents: set[str] = set()
    for row in authorized_locations:
        parent = _safe_str(row.get("parent_warehouse"))
        if parent:
            parents.add(parent)
    return parents


def _warehouse_is_same_or_descendant(warehouse: str | None, possible_ancestor: str | None) -> bool:
    """Return True when warehouse is equal to or below possible_ancestor in the Warehouse tree."""

    warehouse = _safe_str(warehouse)
    possible_ancestor = _safe_str(possible_ancestor)

    if not warehouse or not possible_ancestor:
        return False
    if warehouse == possible_ancestor:
        return True
    if not _has_doctype("Warehouse"):
        return False

    try:
        warehouse_row = frappe.db.get_value("Warehouse", warehouse, ["lft", "rgt"], as_dict=True)
        ancestor_row = frappe.db.get_value("Warehouse", possible_ancestor, ["lft", "rgt"], as_dict=True)
    except Exception:
        return False

    if not warehouse_row or not ancestor_row:
        return False

    try:
        return (
            cint(warehouse_row.get("lft")) >= cint(ancestor_row.get("lft"))
            and cint(warehouse_row.get("rgt")) <= cint(ancestor_row.get("rgt"))
        )
    except Exception:
        return False


def _campaign_matches_authorized_locations(
    campaign_warehouse: str | None,
    authorized_locations: list[dict[str, Any]],
) -> bool:
    """Check whether a campaign warehouse is in the agent location scope.

    Inventory Campaign.warehouse is the campaign parent/site. Inventory Agent
    locations are usually child Warehouses/emplacements. Therefore a campaign
    is available when its warehouse is either the authorized location itself,
    the stored parent warehouse, or any ancestor of the authorized location.
    """

    campaign_warehouse = _safe_str(campaign_warehouse)
    if not campaign_warehouse:
        return False

    # If locations are not configured, keep the historical permissive behavior.
    # The main validation path still blocks the agent before accepting access.
    if not authorized_locations:
        return True

    for row in authorized_locations:
        location_warehouse = _safe_str(row.get("location_warehouse"))
        parent_warehouse = _safe_str(row.get("parent_warehouse"))

        if campaign_warehouse in {location_warehouse, parent_warehouse}:
            return True

        if _warehouse_is_same_or_descendant(location_warehouse, campaign_warehouse):
            return True

        if parent_warehouse and _warehouse_is_same_or_descendant(parent_warehouse, campaign_warehouse):
            return True

    return False




def _campaign_row_to_mobile_summary(campaign_row: Any) -> dict[str, Any]:
    campaign_warehouse = _safe_str(campaign_row.get("warehouse"))
    branch = _safe_str(campaign_row.get("branch"))
    branch_label = _get_branch_label(branch) if branch else None

    settings_erpnext_site = _safe_str(_get_settings_value("erpnext_site"))
    if not settings_erpnext_site:
        settings_erpnext_site = _safe_str(getattr(frappe.local, "site", None))

    settings_server_url = _get_server_url()
    settings_site_url = _safe_str(_get_settings_value("site_url")) or settings_server_url

    return {
        "name": campaign_row.get("name"),
        "campaign_name": campaign_row.get("campaign_name"),
        "company": campaign_row.get("company"),
        "warehouse": campaign_warehouse,
        "warehouse_name": _warehouse_name(campaign_warehouse),
        "branch": branch,
        "branch_name": branch_label,
        "site": branch,
        "site_name": branch_label,
        "status": campaign_row.get("status"),
        "start_date": str(campaign_row.get("start_date")) if campaign_row.get("start_date") else None,
        "end_date": str(campaign_row.get("end_date")) if campaign_row.get("end_date") else None,
        "erpnext_site": settings_erpnext_site,
        "site_url": settings_site_url,
        "server_url": settings_server_url,
        "server_reachable_url": settings_server_url,
        "base_url": settings_server_url,
    }


def _get_active_campaign_for_branch(agent_doc: Any, branch: str | None) -> dict[str, Any]:
    branch = _safe_str(branch)
    if not branch:
        return {
            "ok": False,
            "found": False,
            "reason": "Branch/site is required.",
            "campaign": None,
        }

    if not _has_doctype("Inventory Campaign"):
        return {
            "ok": False,
            "found": False,
            "reason": "Inventory Campaign DocType is not available.",
            "campaign": None,
        }

    if not _has_field("Inventory Campaign", "branch"):
        return {
            "ok": False,
            "found": False,
            "reason": "Inventory Campaign has no branch field.",
            "campaign": None,
        }

    filters: dict[str, Any] = {
        "branch": branch,
        "status": "Open",
    }

    agent_company = _safe_str(_doc_get(agent_doc, "company"))
    if agent_company:
        filters["company"] = agent_company

    fields = [
        "name",
        "campaign_name",
        "company",
        "warehouse",
        "branch",
        "status",
        "start_date",
        "end_date",
    ]

    try:
        campaigns = frappe.get_all(
            "Inventory Campaign",
            filters=filters,
            fields=fields,
            order_by="modified desc",
            limit_page_length=0,
        )
    except Exception:
        frappe.log_error(
            title="Inventory Campaign - active_campaign_by_branch_failed",
            message=frappe.get_traceback(),
        )
        return {
            "ok": False,
            "found": False,
            "reason": "Could not read active campaign for selected branch.",
            "campaign": None,
        }

    if not campaigns:
        return {
            "ok": True,
            "found": False,
            "reason": f"No Open Inventory Campaign found for branch {branch}.",
            "campaign": None,
            "branch": branch,
        }

    if len(campaigns) > 1:
        return {
            "ok": False,
            "found": False,
            "reason": f"Multiple Open Inventory Campaigns found for branch {branch}. Keep only one active campaign per site.",
            "campaign": None,
            "branch": branch,
            "campaigns": [_campaign_row_to_mobile_summary(row) for row in campaigns],
        }

    return {
        "ok": True,
        "found": True,
        "reason": None,
        "branch": branch,
        "campaign": _campaign_row_to_mobile_summary(campaigns[0]),
    }

def _get_available_campaigns(agent_doc: Any, authorized_locations: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return Open campaigns available for the agent company.

    The mobile user now selects the Inventory Campaign directly from a bottom
    sheet. Branch/site selection is no longer used to choose the campaign; the
    campaign already carries its branch/site information.
    """

    if not _has_doctype("Inventory Campaign"):
        return []

    agent_company = _safe_str(_doc_get(agent_doc, "company"))

    filters: dict[str, Any] = {"status": "Open"}
    if agent_company:
        filters["company"] = agent_company

    settings_erpnext_site = _safe_str(_get_settings_value("erpnext_site"))
    if not settings_erpnext_site:
        settings_erpnext_site = _safe_str(getattr(frappe.local, "site", None))

    settings_server_url = _get_server_url()
    settings_site_url = _safe_str(_get_settings_value("site_url")) or settings_server_url

    try:
        campaigns = frappe.get_all(
            "Inventory Campaign",
            filters=filters,
            fields=[
                "name",
                "campaign_name",
                "company",
                "warehouse",
                *(["branch"] if _has_field("Inventory Campaign", "branch") else []),
                "status",
                "start_date",
                "end_date",
            ],
            order_by="modified desc",
        )
    except Exception:
        frappe.log_error(
            title="Inventory Campaign - available_campaigns_failed",
            message=frappe.get_traceback(),
        )
        return []

    result = []
    for campaign_row in campaigns:
        result.append(_campaign_row_to_mobile_summary(campaign_row))

    return result



def _build_mobile_context(agent_doc: Any, campaign: str | None = None) -> dict[str, Any]:
    """Build a lightweight mobile context.

    Do not include the ERPNext Item catalog here. The mobile stores only the
    codification referential and validates each scanned barcode online.
    """

    authorized_locations = _get_authorized_locations(agent_doc)
    authorized_item_groups = _get_authorized_item_groups(agent_doc)
    authorized_item_count = _count_authorized_items(agent_doc)
    available_campaigns = _get_available_campaigns(agent_doc, authorized_locations)
    available_branches = _get_all_branches(agent_doc)

    campaign = _safe_str(campaign)
    selected_campaign = None

    if campaign:
        for row in available_campaigns:
            if row.get("name") == campaign:
                selected_campaign = row
                break

    codification_referential = _get_codification_referential()
    plant_machine_referential = {
        "plant_floors": codification_referential.get("plant_floors") or [],
        "plants": codification_referential.get("plants") or [],
        "workstations": codification_referential.get("workstations") or [],
        "machines": codification_referential.get("machines") or [],
        "version": codification_referential.get("version"),
        "hash": codification_referential.get("hash"),
    }

    return {
        "inventory_agent": _get_agent_context(agent_doc),
        "server_url": _get_server_url(),
        "server_reachable_url": _get_server_url(),
        "base_url": _get_server_url(),
        "selected_campaign": selected_campaign,
        "available_campaigns": available_campaigns,
        "available_branches": available_branches,
        "branches": available_branches,
        "authorized_locations": authorized_locations,
        "authorized_item_groups": authorized_item_groups,
        "authorized_items": [],
        "authorized_item_count": authorized_item_count,
        "item_validation_mode": "online_per_scan",
        "codification_referential": codification_referential,
        "plant_machine_referential": plant_machine_referential,
        "technical_context_referential": plant_machine_referential,
        "referential_version": codification_referential.get("version"),
        "referential_hash": codification_referential.get("hash"),
        "recoding": _get_recoding_mobile_config(),
        "rules": {
            "server_creates_session_now": False,
            "mobile_creates_local_session": True,
            "item_catalog_downloaded_to_mobile": False,
            "scan_item_validation": "online_erpnext",
            "mobile_stores_only_scanned_items": True,
            "unplanned_items_storage": "Inventory Session.unplanned_items_json",
            "unplanned_warehouses_storage": "Inventory Session.unplanned_warehouses_json",
            "recoding_storage": "Inventory Session Item.recoding_tags_json",
            "recoding_is_optional": True,
            "external_agents_define_famille_sous_famille_caracteristiques_plant_machine": True,
            "plant_machine_are_usage_context_not_item_code": True,
            "external_agents_define_only_famille_and_sous_famille": False,
            "mobile_uses_controlled_codification_lists": True,
            "mobile_creates_master_data": False,
            "auto_create_item": False,
            "auto_create_warehouse": False,
            "auto_create_item_group": False,
            "auto_update_item_master_from_mobile": False,
        },
    }


# -----------------------------------------------------------------------------
# Mobile credential helpers
# -----------------------------------------------------------------------------


def _sign_payload(payload: dict[str, Any]) -> str:
    payload_b64 = _base64_urlsafe_encode(_json_dumps(payload).encode("utf-8"))
    signature = hmac.new(
        _get_server_secret(),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_b64}.{signature}"



def _make_mobile_credential(
    agent_doc: Any,
    campaign: str | None,
    device_id: str | None,
) -> tuple[str, dict[str, Any]]:
    ttl_minutes = _get_mobile_credential_ttl(agent_doc)
    now_value = now_datetime()
    expires_at = add_to_date(now_value, minutes=ttl_minutes)

    root_valid_until = _doc_get(agent_doc, "token_valid_until")
    if root_valid_until:
        root_until_dt = get_datetime(root_valid_until)
        if root_until_dt < expires_at:
            expires_at = root_until_dt

    payload = {
        "protocol": MOBILE_CREDENTIAL_PROTOCOL,
        "iat": now_value.isoformat(),
        "valid_from": now_value.isoformat(),
        "exp": expires_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "ttl_minutes": ttl_minutes,
        "jti": secrets.token_urlsafe(16),
        "inventory_agent": agent_doc.name,
        "agent_code": _doc_get(agent_doc, "agent_code"),
        "token_valid_from": _iso_datetime(_doc_get(agent_doc, "token_valid_from")),
        "token_valid_until": _iso_datetime(_doc_get(agent_doc, "token_valid_until")),
        "campaign": _safe_str(campaign),
        "device_id": _safe_str(device_id),
    }

    return _sign_payload(payload), payload



def _consume_agent_token_after_credential(
    agent_doc: Any,
    credential_payload: dict[str, Any],
    device_id: str | None = None,
) -> None:
    """Mark the clear QR token as single-use once a mobile credential is issued."""

    consumed_at = now_datetime()
    _set_agent_password(agent_doc, None)
    agent_doc.agent_token_hash = None
    agent_doc.token_status = "Consumed"
    agent_doc.last_token_used_at = consumed_at
    _doc_set_if_field(agent_doc, "token_consumed_at", consumed_at)
    _doc_set_if_field(agent_doc, "token_consumed_by_device", _safe_str(device_id))
    _doc_set_if_field(agent_doc, "credential_issued_at", credential_payload.get("iat"))
    _doc_set_if_field(agent_doc, "credential_expires_at", credential_payload.get("exp"))



def verify_mobile_credential_payload(
    mobile_credential: str | None,
    expected_agent: str | None = None,
    expected_campaign: str | None = None,
    expected_device_id: str | None = None,
) -> dict[str, Any]:
    mobile_credential = _safe_str(mobile_credential)

    if not mobile_credential or "." not in mobile_credential:
        return {
            "valid": False,
            "reason": "Mobile credential is missing or malformed",
            "payload": None,
        }

    payload_b64, signature = mobile_credential.rsplit(".", 1)
    expected_signature = hmac.new(
        _get_server_secret(),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        return {
            "valid": False,
            "reason": "Mobile credential signature is invalid",
            "payload": None,
        }

    try:
        payload = frappe.parse_json(_base64_urlsafe_decode(payload_b64).decode("utf-8"))
    except Exception:
        return {
            "valid": False,
            "reason": "Mobile credential payload is invalid",
            "payload": None,
        }

    if payload.get("protocol") != MOBILE_CREDENTIAL_PROTOCOL:
        return {
            "valid": False,
            "reason": "Mobile credential protocol is invalid",
            "payload": payload,
        }

    expires_at = get_datetime(payload.get("exp")) if payload.get("exp") else None
    if expires_at and now_datetime() > expires_at:
        return {
            "valid": False,
            "reason": "Mobile credential has expired",
            "payload": payload,
        }

    expected_agent = _safe_str(expected_agent)
    if expected_agent and payload.get("inventory_agent") != expected_agent:
        return {
            "valid": False,
            "reason": "Mobile credential agent mismatch",
            "payload": payload,
        }

    expected_campaign = _safe_str(expected_campaign)
    if expected_campaign and payload.get("campaign") and payload.get("campaign") != expected_campaign:
        return {
            "valid": False,
            "reason": "Mobile credential campaign mismatch",
            "payload": payload,
        }

    expected_device_id = _safe_str(expected_device_id)
    if expected_device_id and payload.get("device_id") and payload.get("device_id") != expected_device_id:
        return {
            "valid": False,
            "reason": "Mobile credential device mismatch",
            "payload": payload,
        }

    return {
        "valid": True,
        "reason": None,
        "payload": payload,
    }


@frappe.whitelist(allow_guest=True)
def verify_mobile_credential(
    mobile_credential: str | None = None,
    campaign: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    result = verify_mobile_credential_payload(
        mobile_credential=mobile_credential,
        expected_campaign=campaign,
        expected_device_id=device_id,
    )

    return {
        "ok": True,
        "valid": bool(result.get("valid")),
        "reason": result.get("reason"),
        "payload": result.get("payload"),
    }


@frappe.whitelist(allow_guest=True)
def get_active_campaign_for_branch(
    mobile_credential: str | None = None,
    branch: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Return the single Open Inventory Campaign for the selected ERPNext Branch.

    The mobile first receives all Branch records from ERPNext, lets the user
    select one, then calls this endpoint to obtain the precise campaign number
    that the local counting session must be attached to.
    """

    verification = verify_mobile_credential_payload(
        mobile_credential=mobile_credential,
        expected_device_id=device_id,
    )

    if not verification.get("valid"):
        return {
            "ok": False,
            "valid": False,
            "found": False,
            "reason": verification.get("reason"),
            "campaign": None,
        }

    payload = verification.get("payload") or {}
    agent_name = _safe_str(payload.get("inventory_agent"))

    if not agent_name or not frappe.db.exists("Inventory Agent", agent_name):
        return {
            "ok": False,
            "valid": False,
            "found": False,
            "reason": "Inventory Agent from mobile credential was not found.",
            "campaign": None,
        }

    agent_doc = frappe.get_doc("Inventory Agent", agent_name)

    result = _get_active_campaign_for_branch(agent_doc, branch)
    result.update({
        "valid": True,
        "inventory_agent": agent_name,
    })
    return result


# -----------------------------------------------------------------------------
# Public mobile APIs
# -----------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def validate_agent_access_token(
    token: str | None = None,
    campaign: str | None = None,
    device_id: str | None = None,
    ssid: str | None = None,
    ip_address: str | None = None,
) -> dict[str, Any]:
    """
    Validate Inventory Agent root token and return a mobile context.

    This endpoint is intentionally strict about the token itself, even when
    security_mode is Disabled. Disabled/Audit/Enforced only controls the
    progressive network/security enforcement layer, not the fact that this
    endpoint is a token-validation endpoint.
    """

    token = _safe_str(token)
    campaign = _safe_str(campaign)
    device_id = _safe_str(device_id)
    ssid = _safe_str(ssid)
    ip_address = _safe_str(ip_address)

    context = _get_security_context()
    security_mode = context.get("security_mode") or "Disabled"
    require_agent_token = _safe_bool(_get_settings_value("require_agent_token", 0))
    require_network_check = bool(context.get("require_network_check"))
    effective_network_required = bool(context.get("effective_require_network_check"))
    audit_network_required = bool(context.get("audit_network_check"))

    token_valid = False
    access_allowed = False
    reason = None
    event_type = "AGENT_TOKEN_MISSING"
    agent_doc = None
    credential = None
    credential_payload = None
    mobile_context = None
    failures: list[dict[str, Any]] = []

    if not token:
        reason = "Agent token is missing"
    else:
        agent_doc = _find_agent_by_token(token)
        if not agent_doc:
            reason = "Agent token is invalid"
            event_type = "AGENT_TOKEN_INVALID"
        elif _doc_get(agent_doc, "status") != "Active":
            reason = f"Inventory Agent status is {_doc_get(agent_doc, 'status')}"
            event_type = "AGENT_DISABLED"
        elif _doc_get(agent_doc, "token_status") != "Active":
            reason = f"Agent token status is {_doc_get(agent_doc, 'token_status')}"
            event_type = "AGENT_TOKEN_NOT_ACTIVE"
        elif not _date_is_within(
            valid_from=_doc_get(agent_doc, "token_valid_from"),
            valid_until=_doc_get(agent_doc, "token_valid_until"),
        ):
            reason = "Agent token is outside its validity period"
            event_type = "AGENT_TOKEN_EXPIRED"
        else:
            bind_to_first_device = _safe_bool(_doc_get(agent_doc, "bind_to_first_device"))
            bound_device_id = _safe_str(_doc_get(agent_doc, "bound_device_id"))

            if bind_to_first_device and not device_id:
                reason = "Device ID is required because this token is configured for device binding"
                event_type = "AGENT_TOKEN_DEVICE_REQUIRED"
            elif bind_to_first_device and bound_device_id and bound_device_id != device_id:
                reason = "Agent token is already bound to another device"
                event_type = "AGENT_TOKEN_DEVICE_MISMATCH"
            else:
                mobile_context = _build_mobile_context(agent_doc, campaign=campaign)
                authorized_locations = mobile_context.get("authorized_locations") or []
                authorized_item_groups = mobile_context.get("authorized_item_groups") or []
                authorized_item_count = _safe_int(mobile_context.get("authorized_item_count"), 0)
                available_campaigns = mobile_context.get("available_campaigns") or []

                if not authorized_item_groups:
                    reason = "Inventory Agent has no selected Authorized Item Groups"
                    event_type = "AGENT_AUTHORIZED_ITEM_GROUPS_EMPTY"
                elif authorized_item_count <= 0:
                    reason = "Inventory Agent has no active stock Items in the selected Authorized Item Groups"
                    event_type = "AGENT_AUTHORIZED_ITEMS_EMPTY"
                elif campaign and not mobile_context.get("selected_campaign"):
                    reason = "Requested campaign is not available for this Inventory Agent scope"
                    event_type = "AGENT_CAMPAIGN_NOT_AVAILABLE"
                else:
                    token_valid = True
                    access_allowed = True
                    reason = None
                    event_type = "AGENT_TOKEN_VALID"

    network_validation = _validate_network(ip_address=ip_address, ssid=ssid)
    network_valid = bool(network_validation.get("valid"))

    if not token_valid:
        failures.append({
            "check": "agent_token",
            "reason": reason,
            "event_type": event_type,
        })

    if (require_network_check or effective_network_required or audit_network_required) and not network_valid:
        failures.append({
            "check": "network",
            "reason": network_validation.get("reason"),
            "event_type": network_validation.get("event_type"),
        })

    if effective_network_required and not network_valid:
        access_allowed = False

    if token_valid and access_allowed and agent_doc:
        bind_to_first_device = _safe_bool(_doc_get(agent_doc, "bind_to_first_device"))
        bound_device_id = _safe_str(_doc_get(agent_doc, "bound_device_id"))
        if bind_to_first_device and device_id and not bound_device_id:
            agent_doc.bound_device_id = device_id
            agent_doc.bound_at = now_datetime()

        credential, credential_payload = _make_mobile_credential(
            agent_doc=agent_doc,
            campaign=campaign,
            device_id=device_id,
        )

        # Single-use QR rule: once the credential has been issued, the clear
        # agent token is consumed and cannot be reused on another device.
        _consume_agent_token_after_credential(
            agent_doc=agent_doc,
            credential_payload=credential_payload,
            device_id=device_id,
        )
        agent_doc.save(ignore_permissions=True)
        frappe.db.commit()

    log_name = None
    if context.get("log_security_events"):
        log_name = _log_security_event(
            event_type=event_type,
            status="Success" if access_allowed else "Blocked",
            security_mode=security_mode,
            campaign=campaign,
            inventory_agent=(agent_doc.name if agent_doc else None),
            device_id=device_id,
            ip_address=network_validation.get("ip_address") or ip_address,
            ssid=network_validation.get("ssid") or ssid,
            message=reason or "Inventory Agent token validated",
            payload={
                "valid": token_valid,
                "access_allowed": access_allowed,
                "require_agent_token": require_agent_token,
                "require_network_check": require_network_check,
                "effective_network_required": effective_network_required,
                "network_valid": network_valid,
                "network_reason": network_validation.get("reason"),
                "failures": failures,
                "note": "Clear agent token is intentionally not logged.",
            },
        )

    response = {
        "ok": True,
        "valid": token_valid,
        "access_allowed": access_allowed,
        "reason": reason,
        "security_mode": security_mode,
        "require_agent_token": require_agent_token,
        "require_network_check": require_network_check,
        "effective_require_network_check": effective_network_required,
        "network_valid": network_valid,
        "network_reason": network_validation.get("reason"),
        "failures": failures,
        "log": log_name,
        "server_creates_session_now": False,
        "next_step": "create_local_mobile_session" if access_allowed else None,
    }

    if agent_doc:
        response.update({
            "inventory_agent": _get_agent_context(agent_doc),
            "token_status": _doc_get(agent_doc, "token_status"),
            "token_valid_from": _iso_datetime(_doc_get(agent_doc, "token_valid_from")),
            "token_valid_until": _iso_datetime(_doc_get(agent_doc, "token_valid_until")),
            "bind_to_first_device": _safe_bool(_doc_get(agent_doc, "bind_to_first_device")),
            "bound_device_id": _doc_get(agent_doc, "bound_device_id"),
        })

    if access_allowed and mobile_context:
        response.update(mobile_context)
        response.update({
            "mobile_credential": credential,
            "mobile_credential_payload": credential_payload,
            "credential_valid_from": credential_payload.get("valid_from") if credential_payload else None,
            "credential_expires_at": credential_payload.get("exp") if credential_payload else None,
            "token_consumed": True,
        })

    return response


@frappe.whitelist(allow_guest=True)
def get_inventory_context(
    mobile_credential: str | None = None,
    campaign: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """
    Return the mobile context after validating the temporary mobile credential.
    """

    verification = verify_mobile_credential_payload(
        mobile_credential=mobile_credential,
        expected_campaign=campaign,
        expected_device_id=device_id,
    )

    if not verification.get("valid"):
        return {
            "ok": False,
            "valid": False,
            "access_allowed": False,
            "reason": verification.get("reason"),
        }

    payload = verification.get("payload") or {}
    agent_name = payload.get("inventory_agent")

    if not agent_name or not frappe.db.exists("Inventory Agent", agent_name):
        return {
            "ok": False,
            "valid": False,
            "access_allowed": False,
            "reason": "Inventory Agent from credential does not exist",
        }

    agent_doc = frappe.get_doc("Inventory Agent", agent_name)
    if _doc_get(agent_doc, "status") != "Active":
        return {
            "ok": False,
            "valid": False,
            "access_allowed": False,
            "reason": f"Inventory Agent status is {_doc_get(agent_doc, 'status')}",
        }

    context_campaign = _safe_str(campaign) or _safe_str(payload.get("campaign"))
    mobile_context = _build_mobile_context(agent_doc, campaign=context_campaign)

    return {
        "ok": True,
        "valid": True,
        "access_allowed": True,
        "mobile_credential_payload": payload,
        **mobile_context,
    }


def _find_item_code_by_scan_value(scan_value: str | None) -> str | None:
    scan_value = _safe_str(scan_value)
    if not scan_value:
        return None

    # 1) Standard ERPNext Item Barcode child table.
    if _has_doctype("Item Barcode"):
        try:
            parent = frappe.db.get_value(
                "Item Barcode",
                {"barcode": scan_value, "parenttype": "Item"},
                "parent",
            )
            parent = _safe_str(parent)
            if parent:
                return parent
        except Exception:
            frappe.log_error(
                title="Inventory Campaign - item_barcode_lookup_failed",
                message=frappe.get_traceback(),
            )

    # 2) Direct Item name / item_code fallback.
    if frappe.db.exists("Item", scan_value):
        return scan_value

    try:
        item_name = frappe.db.get_value("Item", {"item_code": scan_value}, "name")
        return _safe_str(item_name)
    except Exception:
        return None


def _build_scanned_item_response(item_code: str, scanned_code: str | None = None) -> dict[str, Any]:
    item_ctx = _get_item_context(item_code)
    codification = _get_item_codification_context(item_code)

    barcode = _safe_str(scanned_code)
    if not barcode:
        barcodes = item_ctx.get("barcodes") or []
        if barcodes:
            barcode = _safe_str(barcodes[0].get("barcode"))
    if not barcode:
        barcode = item_ctx.get("item_code")

    return {
        **item_ctx,
        "barcode": barcode,
        "uom": item_ctx.get("stock_uom"),
        "codification": codification,
        "current_codification": codification,
        "famille": codification.get("famille"),
        "sous_famille": codification.get("sous_famille"),
        "caracteristiques": codification.get("caracteristiques") or [],
        "active": 1,
    }


@frappe.whitelist(allow_guest=True)
def validate_scanned_item(
    mobile_credential: str | None = None,
    barcode: str | None = None,
    scanned_code: str | None = None,
    campaign: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Validate one scanned barcode/code against ERPNext in real time.

    The mobile no longer downloads the full Item catalog. Each scan calls this
    endpoint, ERPNext resolves the Item, checks Inventory Agent scope through
    authorized_item_groups, and returns only the single Item needed for the
    current count line.
    """

    scan_value = _safe_str(barcode) or _safe_str(scanned_code)
    if not scan_value:
        return {
            "ok": False,
            "valid": False,
            "found": False,
            "allowed": False,
            "reason": "Barcode or item code is required.",
        }

    verification = verify_mobile_credential_payload(
        mobile_credential=mobile_credential,
        expected_campaign=campaign,
        expected_device_id=device_id,
    )
    if not verification.get("valid"):
        return {
            "ok": False,
            "valid": False,
            "found": False,
            "allowed": False,
            "reason": verification.get("reason") or "Mobile credential is invalid.",
        }

    payload = verification.get("payload") or {}
    agent_name = _safe_str(payload.get("inventory_agent"))
    if not agent_name or not frappe.db.exists("Inventory Agent", agent_name):
        return {
            "ok": False,
            "valid": False,
            "found": False,
            "allowed": False,
            "reason": "Inventory Agent from credential does not exist.",
        }

    agent_doc = frappe.get_doc("Inventory Agent", agent_name)
    if _doc_get(agent_doc, "status") != "Active":
        return {
            "ok": False,
            "valid": False,
            "found": False,
            "allowed": False,
            "reason": f"Inventory Agent status is {_doc_get(agent_doc, 'status')}",
        }

    item_code = _find_item_code_by_scan_value(scan_value)
    if not item_code:
        return {
            "ok": True,
            "valid": True,
            "found": False,
            "allowed": False,
            "barcode": scan_value,
            "reason": "Barcode or Item code was not found in ERPNext.",
        }

    item = frappe.db.get_value(
        "Item",
        item_code,
        ["name", "item_code", "item_name", "item_group", "stock_uom", "disabled", "is_stock_item"],
        as_dict=True,
    )
    if not item:
        return {
            "ok": True,
            "valid": True,
            "found": False,
            "allowed": False,
            "barcode": scan_value,
            "reason": "Item was not found in ERPNext.",
        }

    if cint(item.get("disabled")):
        return {
            "ok": True,
            "valid": True,
            "found": True,
            "allowed": False,
            "barcode": scan_value,
            "item_code": item.get("name"),
            "reason": "Item is disabled in ERPNext.",
        }

    if _has_field("Item", "is_stock_item") and not cint(item.get("is_stock_item")):
        return {
            "ok": True,
            "valid": True,
            "found": True,
            "allowed": False,
            "barcode": scan_value,
            "item_code": item.get("name"),
            "reason": "Item is not a stock item.",
        }

    if not _item_is_authorized_for_agent(agent_doc, item.get("item_group")):
        return {
            "ok": True,
            "valid": True,
            "found": True,
            "allowed": False,
            "barcode": scan_value,
            "item_code": item.get("name"),
            "item_group": item.get("item_group"),
            "authorized_item_groups": _get_authorized_item_group_names(agent_doc),
            "reason": "Item Group is not authorized for this Inventory Agent.",
        }

    return {
        "ok": True,
        "valid": True,
        "found": True,
        "allowed": True,
        "barcode": scan_value,
        "inventory_agent": _get_agent_context(agent_doc),
        "item": _build_scanned_item_response(item.get("name") or item_code, scanned_code=scan_value),
    }


@frappe.whitelist(allow_guest=True)
def get_campaign_items(
    mobile_credential: str | None = None,
    campaign: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Deprecated. The mobile no longer downloads the full Item catalog.

    Use validate_scanned_item for online item lookup/authorization per scan.
    """

    context = get_inventory_context(
        mobile_credential=mobile_credential,
        campaign=campaign,
        device_id=device_id,
    )

    if not context.get("ok"):
        return context

    return {
        "ok": True,
        "valid": True,
        "campaign": context.get("selected_campaign"),
        "inventory_agent": context.get("inventory_agent"),
        "authorized_items": [],
        "count": 0,
        "authorized_item_count": context.get("authorized_item_count"),
        "item_validation_mode": "online_per_scan",
        "reason": "Full Item catalog download is disabled. Use validate_scanned_item per barcode scan.",
    }


@frappe.whitelist(allow_guest=True)
def get_campaign_summary(
    mobile_credential: str | None = None,
    campaign: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    context = get_inventory_context(
        mobile_credential=mobile_credential,
        campaign=campaign,
        device_id=device_id,
    )

    if not context.get("ok"):
        return context

    return {
        "ok": True,
        "valid": True,
        "inventory_agent": context.get("inventory_agent"),
        "selected_campaign": context.get("selected_campaign"),
        "available_campaign_count": len(context.get("available_campaigns") or []),
        "authorized_location_count": len(context.get("authorized_locations") or []),
        "authorized_item_count": context.get("authorized_item_count") or 0,
        "rules": context.get("rules"),
    }
