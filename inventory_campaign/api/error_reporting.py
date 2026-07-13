# inventory_campaign/inventory_campaign/api/error_reporting.py

"""Error reporting helpers for the Inventory Campaign mobile app.

Goals:
- return a structured, agent-readable error envelope to the mobile app;
- write every relevant mobile/API failure in ERPNext;
- never store clear tokens, credentials, passwords, or base64 photo bodies.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import frappe
from frappe.utils import get_datetime, now_datetime


SENSITIVE_KEY_PARTS = (
    "token",
    "credential",
    "authorization",
    "password",
    "secret",
    "api_key",
    "api_secret",
    "sid",
)
PHOTO_FIELDNAMES = {"photo_1", "photo_2", "photo_3", "image", "image_data"}
MAX_STRING_LENGTH = 1200
MAX_TRACEBACK_LENGTH = 6000


# -----------------------------------------------------------------------------
# Safe helpers
# -----------------------------------------------------------------------------


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None



def _safe_json_loads(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return frappe.parse_json(value)
        except Exception:
            try:
                return json.loads(value)
            except Exception:
                return value
    return value



def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)



def _truncate(value: Any, limit: int = MAX_STRING_LENGTH) -> Any:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…[truncated {len(text) - limit} chars]"



def _looks_sensitive_key(key: str) -> bool:
    key = (key or "").lower()
    return any(part in key for part in SENSITIVE_KEY_PARTS)



def sanitize_for_error_log(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    """Return a JSON-safe value that can be written to ERPNext logs.

    This function is intentionally conservative. Anything that looks like a
    credential or base64 photo payload is replaced before logging.
    """

    if key and (_looks_sensitive_key(key) or key in PHOTO_FIELDNAMES):
        return "[redacted]"

    if depth > 8:
        return "[max_depth_reached]"

    if isinstance(value, dict):
        return {
            str(item_key): sanitize_for_error_log(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [sanitize_for_error_log(item, depth=depth + 1) for item in value]

    if isinstance(value, bytes):
        return f"[bytes:{len(value)}]"

    if isinstance(value, str):
        text = value.strip()
        if text.lower().startswith("data:image/"):
            return "[mobile_photo_payload_omitted]"
        if len(text) > MAX_STRING_LENGTH:
            return _truncate(text)
        return text

    return value



def make_error_id(prefix: str = "ICERR") -> str:
    now_value = now_datetime()
    seed = f"{getattr(frappe.local, 'site', '')}|{now_value.isoformat()}|{frappe.generate_hash(length=16)}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8].upper()
    return f"{prefix}-{now_value.strftime('%Y%m%d-%H%M%S')}-{digest}"



def _iso_datetime(value: Any) -> str | None:
    if not value:
        return None
    try:
        return get_datetime(value).isoformat()
    except Exception:
        return str(value)



def _get_request_path() -> str | None:
    try:
        return getattr(frappe.local.request, "path", None)
    except Exception:
        return None



def _get_request_ip() -> str | None:
    try:
        if getattr(frappe.local, "request_ip", None):
            return frappe.local.request_ip
    except Exception:
        pass

    try:
        request = frappe.local.request
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
        return request.remote_addr
    except Exception:
        return None



def _has_doctype(doctype: str) -> bool:
    try:
        return bool(frappe.db.exists("DocType", doctype))
    except Exception:
        return False



def _has_field(doctype: str, fieldname: str) -> bool:
    if not _has_doctype(doctype):
        return False
    try:
        return bool(frappe.get_meta(doctype).has_field(fieldname))
    except Exception:
        return False


def _existing_link(doctype: str, name: Any) -> str | None:
    """Return ``name`` only when it resolves to an existing linked document.

    Error logging must never fail because a mobile payload contains a missing,
    stale, or malformed Link value. This helper is intentionally defensive and
    returns ``None`` for any lookup problem.
    """

    value = _safe_str(name)
    if not value or not _has_doctype(doctype):
        return None

    try:
        return value if frappe.db.exists(doctype, value) else None
    except Exception:
        return None


def _extract_agent_from_mobile_credential(mobile_credential: str | None) -> dict[str, Any]:
    mobile_credential = _safe_str(mobile_credential)
    if not mobile_credential:
        return {}

    try:
        from inventory_campaign.api.agent import verify_mobile_credential_payload

        verification = verify_mobile_credential_payload(mobile_credential=mobile_credential)
        if not verification.get("valid"):
            return {"credential_valid": False, "credential_reason": verification.get("reason")}
        payload = verification.get("payload") or {}
        return {
            "credential_valid": True,
            "inventory_agent": _safe_str(payload.get("inventory_agent")),
            "campaign": _safe_str(payload.get("campaign")),
            "device_id": _safe_str(payload.get("device_id")),
            "credential_expires_at": _safe_str(payload.get("exp")),
        }
    except Exception:
        return {"credential_valid": False, "credential_reason": "Credential could not be decoded for error logging."}


# -----------------------------------------------------------------------------
# ERPNext log writers
# -----------------------------------------------------------------------------


def log_inventory_error(
    *,
    error_type: str,
    message: str,
    error_id: str | None = None,
    error_code: str | None = None,
    error_stage: str | None = None,
    level: str = "Error",
    campaign: str | None = None,
    session: str | None = None,
    inventory_agent: str | None = None,
    mobile_session_id: str | None = None,
    device_id: str | None = None,
    operator_name: str | None = None,
    ip_address: str | None = None,
    ssid: str | None = None,
    request_path: str | None = None,
    screen: str | None = None,
    action: str | None = None,
    payload: Any = None,
    traceback: str | None = None,
) -> str | None:
    """Log an Inventory Campaign error in ERPNext and return the error_id.

    The function must not raise. If Inventory Security Log cannot be inserted,
    the standard Error Log still receives the failure.
    """

    error_id = _safe_str(error_id) or make_error_id()
    safe_payload = sanitize_for_error_log(_safe_json_loads(payload) or {})
    safe_traceback = _truncate(traceback, MAX_TRACEBACK_LENGTH) if traceback else None

    log_payload = {
        "error_id": error_id,
        "error_code": _safe_str(error_code),
        "error_stage": _safe_str(error_stage),
        "error_type": _safe_str(error_type) or "MOBILE_OR_API_ERROR",
        "level": _safe_str(level) or "Error",
        "screen": _safe_str(screen),
        "action": _safe_str(action),
        "campaign": _safe_str(campaign),
        "inventory_agent": _safe_str(inventory_agent),
        "mobile_session_id": _safe_str(mobile_session_id),
        "device_id": _safe_str(device_id),
        "request_path": _safe_str(request_path) or _get_request_path(),
        "ip_address": _safe_str(ip_address) or _get_request_ip(),
        "event_time": _iso_datetime(now_datetime()),
        "message": _truncate(message, 1500),
        "traceback": safe_traceback,
        "payload": safe_payload,
    }

    title = f"Inventory Campaign - {log_payload['error_type']} - {error_id}"
    error_log_message = _json_dumps(log_payload)

    try:
        frappe.log_error(title=title, message=error_log_message)
    except Exception:
        pass

    if not _has_doctype("Inventory Security Log"):
        return error_id

    try:
        doc_data = {
            "doctype": "Inventory Security Log",
            "event_type": log_payload["error_type"],
            "status": "Failed" if log_payload["level"] in {"Error", "Critical"} else "Warning",
            "event_time": now_datetime(),
            "campaign": _existing_link("Inventory Campaign", campaign),
            "session": _existing_link("Inventory Session", session),
            "operator_name": _safe_str(operator_name),
            "device_id": log_payload["device_id"],
            "ip_address": log_payload["ip_address"],
            "ssid": _safe_str(ssid),
            "request_path": log_payload["request_path"],
            "message": f"{error_id} | {message}",
            "payload_json": _json_dumps(log_payload),
        }

        if _has_field("Inventory Security Log", "inventory_agent"):
            doc_data["inventory_agent"] = _existing_link("Inventory Agent", inventory_agent)
        if _has_field("Inventory Security Log", "mobile_session_id"):
            doc_data["mobile_session_id"] = _safe_str(mobile_session_id)

        doc = frappe.get_doc(doc_data)
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        try:
            frappe.log_error(
                title=f"Inventory Campaign - error_log_insert_failed - {error_id}",
                message=frappe.get_traceback(),
            )
        except Exception:
            pass

    return error_id



def error_response(
    reason: str,
    *,
    error_code: str,
    error_stage: str,
    error_type: str = "API_ERROR",
    log: bool = True,
    technical_message: str | None = None,
    details: Any = None,
    mobile_can_purge: bool = False,
    submitted: bool = False,
    ack: bool = False,
    campaign: str | None = None,
    session: str | None = None,
    inventory_agent: str | None = None,
    mobile_session_id: str | None = None,
    device_id: str | None = None,
    operator_name: str | None = None,
    payload: Any = None,
    traceback: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a consistent error response for the mobile app."""

    error_id = None
    if log:
        error_id = log_inventory_error(
            error_type=error_type,
            error_code=error_code,
            error_stage=error_stage,
            message=technical_message or reason,
            campaign=campaign,
            session=session,
            inventory_agent=inventory_agent,
            mobile_session_id=mobile_session_id,
            device_id=device_id,
            operator_name=operator_name,
            payload={
                "details": details,
                "request_payload": payload,
                "extra": sanitize_for_error_log(extra),
            },
            traceback=traceback,
        )

    response = {
        "ok": False,
        "submitted": submitted,
        "ack": ack,
        "mobile_can_purge": mobile_can_purge,
        "error": True,
        "error_id": error_id,
        "error_code": error_code,
        "error_stage": error_stage,
        "reason": reason,
        "agent_message": reason,
        "technical_message": _truncate(technical_message, 900) if technical_message else None,
        "details": sanitize_for_error_log(details) if details is not None else None,
        "next_step": "keep_mobile_session_and_retry",
    }
    response.update(extra)
    return response


# -----------------------------------------------------------------------------
# Public API used by the Flutter crash/error reporter
# -----------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def report_mobile_error(
    error_type: str | None = None,
    level: str | None = None,
    message: str | None = None,
    stack_trace: str | None = None,
    mobile_credential: str | None = None,
    campaign: str | None = None,
    inventory_agent: str | None = None,
    mobile_session_id: str | None = None,
    device_id: str | None = None,
    screen: str | None = None,
    action: str | None = None,
    payload: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Receive a handled/unhandled mobile error and write it in ERPNext.

    The mobile may call this endpoint after a Flutter exception, API parsing
    problem, or UI failure. The endpoint is allow_guest because the mobile uses
    its signed mobile_credential rather than a full ERPNext user session.
    """

    message = _safe_str(message) or "Mobile app reported an error."
    credential_context = _extract_agent_from_mobile_credential(mobile_credential)

    resolved_inventory_agent = _safe_str(inventory_agent) or _safe_str(credential_context.get("inventory_agent"))
    resolved_campaign = _safe_str(campaign) or _safe_str(credential_context.get("campaign"))
    resolved_device_id = _safe_str(device_id) or _safe_str(credential_context.get("device_id"))

    error_id = log_inventory_error(
        error_type=_safe_str(error_type) or "MOBILE_ERROR",
        level=_safe_str(level) or "Error",
        error_code=_safe_str(kwargs.get("error_code")) or "MOBILE_APP_ERROR",
        error_stage=_safe_str(kwargs.get("error_stage")) or _safe_str(action) or "mobile_runtime",
        message=message,
        campaign=resolved_campaign,
        inventory_agent=resolved_inventory_agent,
        mobile_session_id=_safe_str(mobile_session_id),
        device_id=resolved_device_id,
        screen=_safe_str(screen),
        action=_safe_str(action),
        payload={
            "payload": _safe_json_loads(payload),
            "kwargs": sanitize_for_error_log(kwargs),
            "credential_context": sanitize_for_error_log(credential_context),
        },
        traceback=_safe_str(stack_trace),
    )

    return {
        "ok": True,
        "logged": True,
        "error_id": error_id,
        "message": "Mobile error logged in ERPNext.",
    }
