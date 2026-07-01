# inventory_campaign/inventory_campaign/api/security.py

import base64
import hashlib
import hmac
import ipaddress
import re

import frappe
from frappe.utils import cint
from frappe.utils import add_to_date, get_datetime, now_datetime

DEFAULT_SECURITY_CONTEXT = {
    "security_mode": "Disabled",
    "require_access_token": 0,
    "require_network_check": 0,
    "allow_development_bypass": 1,
    "log_security_events": 1,
    "default_token_validity_hours": 24,
    "max_sessions_per_token": 1,
}


SECURITY_MODE_DISABLED = "Disabled"
SECURITY_MODE_AUDIT_ONLY = "Audit Only"
SECURITY_MODE_ENFORCED = "Enforced"

VALID_SECURITY_MODES = {
    SECURITY_MODE_DISABLED,
    SECURITY_MODE_AUDIT_ONLY,
    SECURITY_MODE_ENFORCED,
}


# ---------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------

def _safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _safe_str(value):
    if value is None:
        return None

    value = str(value).strip()
    return value or None



def _safe_bool(value, default=False):
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


def _has_doctype(doctype):
    return bool(frappe.db.exists("DocType", doctype))


def _has_field(doctype, fieldname):
    if not _has_doctype(doctype):
        return False

    try:
        return frappe.get_meta(doctype).has_field(fieldname)
    except Exception:
        return False


def _doc_get(doc, fieldname, default=None):
    if not doc:
        return default

    try:
        return doc.get(fieldname, default)
    except Exception:
        return getattr(doc, fieldname, default)


def _normalize_protocol(value, default="http"):
    protocol = (_safe_str(value) or default).replace("://", "").replace("/", "").lower()
    return protocol if protocol in {"http", "https"} else default


def _strip_protocol_and_slashes(value):
    value = _safe_str(value)
    if not value:
        return None
    value = re.sub(r"^https?://", "", value, flags=re.I).strip().strip("/")
    return value or None


def _build_server_reachable_url(settings):
    direct = _safe_str(_doc_get(settings, "server_reachable_url"))
    protocol = _normalize_protocol(
        _doc_get(settings, "protocol") or _doc_get(settings, "protocole"),
        default="http",
    )

    if direct:
        direct = direct.rstrip("/")
        if direct.startswith(("http://", "https://")):
            return direct
        host = _strip_protocol_and_slashes(direct)
        return f"{protocol}://{host}" if host else None

    host = _strip_protocol_and_slashes(_doc_get(settings, "server_url"))
    if host:
        return f"{protocol}://{host}"

    try:
        return frappe.utils.get_url().rstrip("/")
    except Exception:
        site = _safe_str(getattr(frappe.local, "site", None))
        return f"{protocol}://{site}" if site else None


def _iso_datetime(value):
    if not value:
        return None

    try:
        return get_datetime(value).isoformat()
    except Exception:
        return str(value)


def _date_is_within(valid_from=None, valid_until=None):
    now_value = now_datetime()

    from_value = get_datetime(valid_from) if valid_from else None
    until_value = get_datetime(valid_until) if valid_until else None

    if from_value and now_value < from_value:
        return False

    if until_value and now_value > until_value:
        return False

    return True


def _get_server_secret():
    secret = frappe.conf.get("encryption_key") or frappe.conf.get("secret_key")

    if not secret:
        secret = getattr(frappe.local, "site", None) or "inventory-campaign-local-secret"

    return str(secret).encode("utf-8")


def _base64_urlsafe_encode(raw_bytes):
    return base64.urlsafe_b64encode(raw_bytes).decode("utf-8").rstrip("=")


def _base64_urlsafe_decode(value):
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("utf-8"))


def _get_request_path():
    try:
        return getattr(frappe.local.request, "path", None)
    except Exception:
        return None


def _get_request_ip():
    """
    Return the best available request IP.

    This is used for logging only in IC-S2-04.
    Network enforcement will be refined in IC-S2-05.
    """

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


def hash_inventory_access_token(token):
    """
    Canonical token hash for Inventory Access Token.

    Only the hash is stored on the server.
    The clear token must never be saved in any DocType or log.
    """

    token = _safe_str(token)

    if not token:
        return None

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _looks_like_sha256(value):
    value = _safe_str(value)

    if not value or len(value) != 64:
        return False

    try:
        int(value, 16)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------
# Security mode / settings
# ---------------------------------------------------------------------

def normalize_security_mode(value):
    """
    Original security mode rules:

    Disabled:
        Security is inactive. No blocking. Minimal or no checks.

    Audit Only:
        Security checks can run and security events can be logged,
        but the user / mobile flow is not blocked.

    Enforced:
        Security checks are active and blocking.
    """

    if value in VALID_SECURITY_MODES:
        return value

    return DEFAULT_SECURITY_CONTEXT["security_mode"]


def is_security_blocking(security_mode):
    return normalize_security_mode(security_mode) == SECURITY_MODE_ENFORCED


def is_security_audit_only(security_mode):
    return normalize_security_mode(security_mode) == SECURITY_MODE_AUDIT_ONLY


def is_security_disabled(security_mode):
    return normalize_security_mode(security_mode) == SECURITY_MODE_DISABLED


def _settings_doctype_exists():
    return frappe.db.exists("DocType", "Inventory Campaign Settings")


def _get_inventory_campaign_settings():
    """
    Return Inventory Campaign Settings as a dict.

    The API must remain usable during early development even if the setup script
    was not executed yet. In that case, we return safe development defaults:
    security_mode = Disabled.
    """

    if not _settings_doctype_exists():
        return {
            **DEFAULT_SECURITY_CONTEXT,
            "settings_available": False,
            "warning": "Inventory Campaign Settings DocType does not exist yet. Using development defaults.",
        }

    try:
        settings = frappe.get_single("Inventory Campaign Settings")
    except Exception as exc:
        return {
            **DEFAULT_SECURITY_CONTEXT,
            "settings_available": False,
            "warning": f"Unable to load Inventory Campaign Settings. Using development defaults. Error: {str(exc)}",
        }

    security_mode = normalize_security_mode(
        settings.get("security_mode") or DEFAULT_SECURITY_CONTEXT["security_mode"]
    )

    return {
        "settings_available": True,
        "security_mode": security_mode,
        "require_access_token": _safe_int(
            settings.get("require_access_token"),
            DEFAULT_SECURITY_CONTEXT["require_access_token"],
        ),
        "require_agent_token": _safe_int(
            settings.get("require_agent_token"),
            settings.get("require_access_token") or DEFAULT_SECURITY_CONTEXT["require_access_token"],
        ),
        "require_network_check": _safe_int(
            settings.get("require_network_check"),
            DEFAULT_SECURITY_CONTEXT["require_network_check"],
        ),
        "allow_development_bypass": _safe_int(
            settings.get("allow_development_bypass"),
            DEFAULT_SECURITY_CONTEXT["allow_development_bypass"],
        ),
        "log_security_events": _safe_int(
            settings.get("log_security_events"),
            DEFAULT_SECURITY_CONTEXT["log_security_events"],
        ),
        "default_token_validity_hours": _safe_int(
            settings.get("default_token_validity_hours"),
            DEFAULT_SECURITY_CONTEXT["default_token_validity_hours"],
        ),
        "max_sessions_per_token": _safe_int(
            settings.get("max_sessions_per_token"),
            DEFAULT_SECURITY_CONTEXT["max_sessions_per_token"],
        ),
        "server_reachable_url": _build_server_reachable_url(settings),
    }


def get_security_context_dict():
    """
    Internal helper used by public APIs and future server-side enforcement code.
    """

    context = _get_inventory_campaign_settings()
    security_mode = context.get("security_mode") or DEFAULT_SECURITY_CONTEXT["security_mode"]

    require_access_token = bool(context.get("require_access_token"))
    require_agent_token = bool(context.get("require_agent_token")) or require_access_token
    require_network_check = bool(context.get("require_network_check"))
    security_blocking = is_security_blocking(security_mode)
    audit_only = is_security_audit_only(security_mode)
    security_disabled = is_security_disabled(security_mode)

    response = {
        "ok": True,
        "settings_available": context.get("settings_available", False),
        "security_mode": security_mode,

        # Main interpretation
        "security_disabled": security_disabled,
        "security_audit_only": audit_only,
        "security_blocking": security_blocking,

        # Configured checks
        "require_access_token": require_access_token,
        "require_agent_token": require_agent_token,
        "require_network_check": require_network_check,

        # Effective blocking behavior
        "effective_require_access_token": security_blocking and (require_access_token or require_agent_token),
        "effective_require_network_check": security_blocking and require_network_check,

        # Audit behavior: checks may be evaluated and logged, but not block.
        "audit_access_token": audit_only and (require_access_token or require_agent_token),
        "audit_network_check": audit_only and require_network_check,

        "allow_development_bypass": bool(context.get("allow_development_bypass")),
        "log_security_events": bool(context.get("log_security_events")),
        "default_token_validity_hours": context.get("default_token_validity_hours"),
        "max_sessions_per_token": context.get("max_sessions_per_token"),
        "server_reachable_url": context.get("server_reachable_url"),
        "server_time": now_datetime().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if context.get("warning"):
        response["warning"] = context.get("warning")

    return response


@frappe.whitelist(allow_guest=True)
def get_inventory_security_context():
    """
    Return the current progressive security context for the mobile app.

    Original rules:
    - Disabled = security inactive, non-blocking.
    - Audit Only = security checks/logs possible, non-blocking.
    - Enforced = security checks are blocking.
    """

    return get_security_context_dict()


# ---------------------------------------------------------------------
# Security log
# ---------------------------------------------------------------------

def _security_log_doctype_exists():
    return frappe.db.exists("DocType", "Inventory Security Log")


def log_security_event(
    event_type,
    status="Success",
    security_mode=None,
    campaign=None,
    session=None,
    access_token=None,
    operator_user=None,
    operator_name=None,
    inventory_agent=None,
    mobile_session_id=None,
    device_id=None,
    ip_address=None,
    ssid=None,
    request_path=None,
    message=None,
    payload=None,
):
    """
    Insert an Inventory Security Log record when available.

    This helper must never raise an error that blocks the mobile flow.
    It also must never store the clear token.
    """

    if not _security_log_doctype_exists():
        return None

    try:
        log_payload = {
            "doctype": "Inventory Security Log",
            "event_type": event_type,
            "status": status,
            "security_mode": security_mode,
            "event_time": now_datetime(),
            "campaign": campaign,
            "session": session,
            "access_token": access_token,
            "operator_user": operator_user,
            "operator_name": operator_name,
            "device_id": device_id,
            "ip_address": ip_address or _get_request_ip(),
            "ssid": ssid,
            "request_path": request_path or _get_request_path(),
            "message": message,
            "payload_json": frappe.as_json(payload or {}),
        }

        if _has_field("Inventory Security Log", "inventory_agent"):
            log_payload["inventory_agent"] = inventory_agent

        if _has_field("Inventory Security Log", "mobile_session_id"):
            log_payload["mobile_session_id"] = mobile_session_id

        doc = frappe.get_doc(log_payload)

        doc.insert(ignore_permissions=True)
        frappe.db.commit()

        return doc.name
    except Exception:
        frappe.log_error(
            title="Inventory Campaign - security_log_failed",
            message=frappe.get_traceback(),
        )
        return None



# ---------------------------------------------------------------------
# Network validation
# ---------------------------------------------------------------------

def _split_network_rules(value):
    """
    Split newline/comma/semicolon separated network rules.

    Supported values:
    - SSIDs: "Warehouse WiFi", "InventoryNet"
    - IP ranges: "192.168.1.0/24", "10.0.0.10", "172.16.0.1-172.16.0.50"
    """

    if not value:
        return []

    parts = re.split(r"[\n,;]+", str(value))
    return [part.strip() for part in parts if part and part.strip()]


def _get_network_rules_from_settings():
    """
    Load internal network rules from Inventory Campaign Settings.

    These rules are intentionally not exposed by get_inventory_security_context().
    """

    if not _settings_doctype_exists():
        return {
            "allowed_ssids": [],
            "allowed_ip_ranges": [],
        }

    try:
        settings = frappe.get_single("Inventory Campaign Settings")
    except Exception:
        return {
            "allowed_ssids": [],
            "allowed_ip_ranges": [],
        }

    return {
        "allowed_ssids": _split_network_rules(settings.get("allowed_ssids")),
        "allowed_ip_ranges": _split_network_rules(settings.get("allowed_ip_ranges")),
    }


def _normalize_ip_address(ip_address=None):
    """
    Normalize the IP address used for validation.

    Preference:
    1. Explicit ip_address argument, useful for tests.
    2. Request IP inferred from headers/request context.

    If X-Forwarded-For contains multiple IPs, only the first is used.
    """

    ip_address = _safe_str(ip_address) or _safe_str(_get_request_ip())

    if not ip_address:
        return None

    # Sometimes proxy headers or manual tests pass multiple IPs.
    if "," in ip_address:
        ip_address = ip_address.split(",")[0].strip()

    return ip_address


def _ip_matches_rule(ip_address, rule):
    """
    Support:
    - Single IP: 192.168.1.10
    - CIDR range: 192.168.1.0/24
    - Explicit range: 192.168.1.10-192.168.1.50
    """

    ip_address = _safe_str(ip_address)
    rule = _safe_str(rule)

    if not ip_address or not rule:
        return False

    try:
        ip = ipaddress.ip_address(ip_address)

        if "-" in rule:
            start_value, end_value = [part.strip() for part in rule.split("-", 1)]
            start_ip = ipaddress.ip_address(start_value)
            end_ip = ipaddress.ip_address(end_value)
            return start_ip <= ip <= end_ip

        if "/" in rule:
            network = ipaddress.ip_network(rule, strict=False)
            return ip in network

        return ip == ipaddress.ip_address(rule)

    except Exception:
        return False


def _ssid_matches_rule(ssid, rule):
    """
    SSID matching is exact and case-sensitive by default.

    That is intentional: SSID is already a weak client-declared signal.
    Making it fuzzy would make it weaker.
    """

    ssid = _safe_str(ssid)
    rule = _safe_str(rule)

    if not ssid or not rule:
        return False

    return ssid == rule


def _validate_network_values(ip_address=None, ssid=None):
    """
    Validate the current network context against Inventory Campaign Settings.

    Rules:
    - If both SSID and IP rules are configured, matching either one is considered
      enough for IC-S2-05. This keeps field testing flexible.
    - If only one family of rules is configured, that family must match.
    - If no network rule is configured, validation fails when network security is
      effectively required. It still remains non-blocking in Disabled/Audit Only.
    """

    rules = _get_network_rules_from_settings()
    allowed_ssids = rules.get("allowed_ssids") or []
    allowed_ip_ranges = rules.get("allowed_ip_ranges") or []

    normalized_ip = _normalize_ip_address(ip_address)
    ssid = _safe_str(ssid)

    ip_match = False
    ssid_match = False

    if normalized_ip and allowed_ip_ranges:
        ip_match = any(_ip_matches_rule(normalized_ip, rule) for rule in allowed_ip_ranges)

    if ssid and allowed_ssids:
        ssid_match = any(_ssid_matches_rule(ssid, rule) for rule in allowed_ssids)

    has_ip_rules = bool(allowed_ip_ranges)
    has_ssid_rules = bool(allowed_ssids)
    has_any_rules = has_ip_rules or has_ssid_rules

    if not has_any_rules:
        return {
            "valid": False,
            "reason": "No network rules configured",
            "event_type": "NETWORK_RULES_MISSING",
            "status": "Failed",
            "ip_address": normalized_ip,
            "ssid": ssid,
            "ip_match": False,
            "ssid_match": False,
            "has_ip_rules": False,
            "has_ssid_rules": False,
        }

    if has_ip_rules and has_ssid_rules:
        valid = ip_match or ssid_match
    elif has_ip_rules:
        valid = ip_match
    else:
        valid = ssid_match

    if valid:
        return {
            "valid": True,
            "reason": None,
            "event_type": "NETWORK_VALID",
            "status": "Success",
            "ip_address": normalized_ip,
            "ssid": ssid,
            "ip_match": ip_match,
            "ssid_match": ssid_match,
            "has_ip_rules": has_ip_rules,
            "has_ssid_rules": has_ssid_rules,
        }

    return {
        "valid": False,
        "reason": "Network is not authorized",
        "event_type": "NETWORK_INVALID",
        "status": "Failed",
        "ip_address": normalized_ip,
        "ssid": ssid,
        "ip_match": ip_match,
        "ssid_match": ssid_match,
        "has_ip_rules": has_ip_rules,
        "has_ssid_rules": has_ssid_rules,
    }


@frappe.whitelist(allow_guest=True)
def validate_inventory_network(
    ip_address=None,
    ssid=None,
    campaign=None,
    session=None,
    device_id=None,
    operator_name=None,
):
    """
    Validate whether the mobile request comes from an authorized network.

    Original security mode rules:
    - Disabled: network validation is non-blocking.
    - Audit Only: network validation is checked/logged but non-blocking.
    - Enforced: network validation blocks when require_network_check = 1.

    Notes:
    - SSID is client-declared and therefore weak.
    - IP source is stronger but can depend on reverse proxy configuration.
    - IC-S2-05 prepares the validation layer; final hardening can refine proxy
      trust rules and mobile-side reachable-server checks.
    """

    context = get_security_context_dict()
    security_mode = context.get("security_mode")
    configured_required = bool(context.get("require_network_check"))
    audit_required = bool(context.get("audit_network_check"))
    effective_required = bool(context.get("effective_require_network_check"))

    campaign = _safe_str(campaign)
    session = _safe_str(session)
    device_id = _safe_str(device_id)
    operator_name = _safe_str(operator_name)

    validation = _validate_network_values(ip_address=ip_address, ssid=ssid)

    valid = bool(validation.get("valid"))
    reason = validation.get("reason")
    event_type = validation.get("event_type")
    status = validation.get("status", "Success" if valid else "Failed")
    normalized_ip = validation.get("ip_address")
    ssid_value = validation.get("ssid")

    access_allowed = True

    if effective_required:
        access_allowed = valid

    if context.get("log_security_events") and (
        configured_required
        or audit_required
        or effective_required
        or normalized_ip
        or ssid_value
    ):
        log_security_event(
            event_type=event_type,
            status=status if valid else ("Blocked" if not access_allowed else "Warning"),
            security_mode=security_mode,
            campaign=campaign,
            session=session,
            operator_name=operator_name,
            device_id=device_id,
            ip_address=normalized_ip,
            ssid=ssid_value,
            message=reason or "Network is valid",
            payload={
                "valid": valid,
                "access_allowed": access_allowed,
                "configured_required": configured_required,
                "audit_required": audit_required,
                "effective_required": effective_required,
                "ip_match": validation.get("ip_match"),
                "ssid_match": validation.get("ssid_match"),
                "has_ip_rules": validation.get("has_ip_rules"),
                "has_ssid_rules": validation.get("has_ssid_rules"),
            },
        )

    return {
        "ok": True,
        "valid": valid,
        "access_allowed": bool(access_allowed),
        "reason": reason,
        "security_mode": security_mode,
        "security_blocking": bool(context.get("security_blocking")),
        "require_network_check": configured_required,
        "audit_network_check": audit_required,
        "effective_require_network_check": effective_required,
        "ip_address": normalized_ip,
        "ssid": ssid_value,
        "ip_match": bool(validation.get("ip_match")),
        "ssid_match": bool(validation.get("ssid_match")),
        "has_ip_rules": bool(validation.get("has_ip_rules")),
        "has_ssid_rules": bool(validation.get("has_ssid_rules")),
    }


# ---------------------------------------------------------------------
# Access token validation
# ---------------------------------------------------------------------

def _find_access_token_doc(token):
    """
    Find Inventory Access Token by hash.

    Normal behavior:
    - Mobile sends clear token.
    - Server hashes it with SHA-256.
    - Server searches token_hash.

    Development convenience:
    - If a 64-char SHA-256 hash is passed directly, we also try it as-is.
      This is useful for bench/manual tests, but the mobile should send the
      clear token in the real flow.
    """

    if not frappe.db.exists("DocType", "Inventory Access Token"):
        return None

    token_hash = hash_inventory_access_token(token)

    if token_hash:
        name = frappe.db.get_value(
            "Inventory Access Token",
            {"token_hash": token_hash},
            "name",
        )
        if name:
            return frappe.get_doc("Inventory Access Token", name)

    token_as_hash = _safe_str(token)

    if _looks_like_sha256(token_as_hash):
        name = frappe.db.get_value(
            "Inventory Access Token",
            {"token_hash": token_as_hash},
            "name",
        )
        if name:
            return frappe.get_doc("Inventory Access Token", name)

    return None


def _validate_access_token_doc(token_doc, expected_campaign=None):
    """
    Validate an Inventory Access Token document.

    This function does not consume the token.
    used_sessions must be incremented later when a real Inventory Session is opened.
    """

    now_value = now_datetime()

    if not token_doc:
        return {
            "valid": False,
            "reason": "Invalid token",
            "event_type": "TOKEN_INVALID",
            "status": "Failed",
        }

    if token_doc.status != "Active":
        event_type = "TOKEN_INVALID_STATUS"

        if token_doc.status == "Expired":
            event_type = "TOKEN_EXPIRED"
        elif token_doc.status == "Revoked":
            event_type = "TOKEN_REVOKED"
        elif token_doc.status == "Used":
            event_type = "TOKEN_USED"

        return {
            "valid": False,
            "reason": f"Token status is {token_doc.status}",
            "event_type": event_type,
            "status": "Failed",
        }

    valid_from = get_datetime(token_doc.valid_from) if token_doc.valid_from else None
    valid_until = get_datetime(token_doc.valid_until) if token_doc.valid_until else None

    if valid_from and now_value < valid_from:
        return {
            "valid": False,
            "reason": "Token is not valid yet",
            "event_type": "TOKEN_NOT_YET_VALID",
            "status": "Failed",
        }

    if valid_until and now_value > valid_until:
        return {
            "valid": False,
            "reason": "Token has expired",
            "event_type": "TOKEN_EXPIRED",
            "status": "Failed",
        }

    max_sessions = _safe_int(token_doc.max_sessions, 0)
    used_sessions = _safe_int(token_doc.used_sessions, 0)

    if max_sessions > 0 and used_sessions >= max_sessions:
        return {
            "valid": False,
            "reason": "Token session limit reached",
            "event_type": "TOKEN_USAGE_LIMIT_REACHED",
            "status": "Failed",
        }

    expected_campaign = _safe_str(expected_campaign)

    if expected_campaign and token_doc.campaign != expected_campaign:
        return {
            "valid": False,
            "reason": "Token does not belong to the requested campaign",
            "event_type": "TOKEN_CAMPAIGN_MISMATCH",
            "status": "Failed",
        }

    return {
        "valid": True,
        "reason": None,
        "event_type": "TOKEN_VALID",
        "status": "Success",
    }


@frappe.whitelist(allow_guest=True)
def validate_inventory_access_token(
    token=None,
    campaign=None,
    device_id=None,
    ssid=None,
    operator_name=None,
):
    """
    Validate an Inventory Access Token.

    This API is safe for early mobile development:
    - Disabled: missing/invalid token does not block access.
    - Audit Only: missing/invalid token is logged but does not block access.
    - Enforced: token becomes blocking only if require_access_token is enabled.

    Important:
    - This API does not consume the token.
    - The token should be consumed later when opening a real Inventory Session.
    - The clear token is never stored in logs.
    """

    context = get_security_context_dict()
    security_mode = context.get("security_mode")
    effective_required = bool(context.get("effective_require_access_token"))
    audit_required = bool(context.get("audit_access_token"))
    configured_required = bool(context.get("require_access_token"))

    token = _safe_str(token)
    campaign = _safe_str(campaign)
    device_id = _safe_str(device_id)
    ssid = _safe_str(ssid)
    operator_name = _safe_str(operator_name)

    if not token:
        valid = False
        reason = "Token is missing"
        event_type = "TOKEN_MISSING"
        status = "Failed"
        token_doc = None
    else:
        try:
            token_doc = _find_access_token_doc(token)
            validation = _validate_access_token_doc(token_doc, expected_campaign=campaign)

            valid = validation["valid"]
            reason = validation.get("reason")
            event_type = validation.get("event_type")
            status = validation.get("status", "Success" if valid else "Failed")
        except Exception:
            token_doc = None
            valid = False
            reason = "Token validation failed because of a server error"
            event_type = "TOKEN_VALIDATION_ERROR"
            status = "Failed"

            frappe.log_error(
                title="Inventory Campaign - token_validation_failed",
                message=frappe.get_traceback(),
            )

    # Access decision for this validation context.
    #
    # If token is not effectively required, an invalid token must not block the
    # business flow. The API still returns valid=False so the mobile can display
    # a warning in Audit Only / testing if desired.
    access_allowed = True

    if effective_required:
        access_allowed = bool(valid)

    if context.get("log_security_events") and (configured_required or audit_required or effective_required or token):
        log_security_event(
            event_type=event_type,
            status=status if valid else ("Blocked" if not access_allowed else "Warning"),
            security_mode=security_mode,
            campaign=(token_doc.campaign if token_doc else campaign),
            access_token=(token_doc.name if token_doc else None),
            operator_user=(token_doc.operator_user if token_doc else None),
            operator_name=(token_doc.operator_name if token_doc and token_doc.operator_name else operator_name),
            device_id=device_id,
            ssid=ssid,
            message=reason or "Token is valid",
            payload={
                "valid": valid,
                "access_allowed": access_allowed,
                "configured_required": configured_required,
                "audit_required": audit_required,
                "effective_required": effective_required,
                "campaign_requested": campaign,
                "token_doc_found": bool(token_doc),
            },
        )

    response = {
        "ok": True,
        "valid": bool(valid),
        "access_allowed": bool(access_allowed),
        "reason": reason,
        "security_mode": security_mode,
        "security_blocking": bool(context.get("security_blocking")),
        "require_access_token": configured_required,
        "audit_access_token": audit_required,
        "effective_require_access_token": effective_required,
    }

    if token_doc:
        max_sessions = _safe_int(token_doc.max_sessions, 0)
        used_sessions = _safe_int(token_doc.used_sessions, 0)

        response.update({
            "access_token": token_doc.name,
            "campaign": token_doc.campaign,
            "company": token_doc.company,
            "warehouse": token_doc.warehouse,
            "operator_user": token_doc.operator_user,
            "operator_name": token_doc.operator_name,
            "valid_from": str(token_doc.valid_from) if token_doc.valid_from else None,
            "valid_until": str(token_doc.valid_until) if token_doc.valid_until else None,
            "max_sessions": max_sessions,
            "used_sessions": used_sessions,
            "sessions_remaining": max(max_sessions - used_sessions, 0) if max_sessions > 0 else None,
        })

    return response


# ---------------------------------------------------------------------
# IC-S5-01: Agent access token validation
# ---------------------------------------------------------------------

AGENT_CAMPAIGN_TOKEN_TYPE = "Agent Campaign Access"
LEGACY_CAMPAIGN_TOKEN_TYPE = "Legacy Campaign Access"


def _get_campaign_context(campaign_name):
    campaign_name = _safe_str(campaign_name)

    if not campaign_name or not frappe.db.exists("Inventory Campaign", campaign_name):
        return None

    campaign = frappe.get_doc("Inventory Campaign", campaign_name)

    warehouse = _doc_get(campaign, "warehouse")
    warehouse_name = frappe.db.get_value("Warehouse", warehouse, "warehouse_name") if warehouse else None

    return {
        "name": campaign.name,
        "campaign_name": _doc_get(campaign, "campaign_name"),
        "company": _doc_get(campaign, "company"),
        "warehouse": warehouse,
        "warehouse_name": warehouse_name,
        "status": _doc_get(campaign, "status"),
        "start_date": str(_doc_get(campaign, "start_date")) if _doc_get(campaign, "start_date") else None,
        "end_date": str(_doc_get(campaign, "end_date")) if _doc_get(campaign, "end_date") else None,
        "erpnext_site": _doc_get(campaign, "erpnext_site"),
        "site_url": _doc_get(campaign, "site_url"),
        "server_reachable_url": _doc_get(campaign, "server_reachable_url"),
    }


def _get_inventory_agent_context(agent_name):
    agent_name = _safe_str(agent_name)

    if not agent_name or not frappe.db.exists("Inventory Agent", agent_name):
        return None

    agent = frappe.get_doc("Inventory Agent", agent_name)

    return {
        "name": agent.name,
        "agent_code": _doc_get(agent, "agent_code"),
        "agent_name": _doc_get(agent, "agent_name"),
        "status": _doc_get(agent, "status"),
        "phone": _doc_get(agent, "phone"),
        "email": _doc_get(agent, "email"),
        "company": _doc_get(agent, "company"),
    }


def _warehouse_context(warehouse):
    warehouse = _safe_str(warehouse)

    if not warehouse:
        return {
            "warehouse": None,
            "warehouse_name": None,
        }

    return {
        "warehouse": warehouse,
        "warehouse_name": frappe.db.get_value("Warehouse", warehouse, "warehouse_name"),
    }


def _is_child_warehouse(location_warehouse, parent_warehouse):
    location_warehouse = _safe_str(location_warehouse)
    parent_warehouse = _safe_str(parent_warehouse)

    if not location_warehouse or not parent_warehouse:
        return None

    try:
        location_parent = frappe.db.get_value(
            "Warehouse",
            location_warehouse,
            "parent_warehouse",
        )
        return location_parent == parent_warehouse
    except Exception:
        return None


def _get_agent_assignments(agent_name, campaign_warehouse=None):
    agent_name = _safe_str(agent_name)
    campaign_warehouse = _safe_str(campaign_warehouse)

    if not agent_name or not frappe.db.exists("Inventory Agent", agent_name):
        return []

    agent = frappe.get_doc("Inventory Agent", agent_name)
    assignments = []

    for row in agent.get("assignments") or []:
        if not cint(_doc_get(row, "active", 1)):
            continue

        if not _date_is_within(
            valid_from=_doc_get(row, "valid_from"),
            valid_until=_doc_get(row, "valid_until"),
        ):
            continue

        parent_warehouse = _safe_str(_doc_get(row, "parent_warehouse"))
        location_warehouse = _safe_str(_doc_get(row, "location_warehouse"))
        item_group = _safe_str(_doc_get(row, "item_group"))

        if campaign_warehouse and parent_warehouse and parent_warehouse != campaign_warehouse:
            # Campaign currently carries one parent warehouse. Assignments outside
            # that parent warehouse are intentionally not returned for this token.
            continue

        parent_ctx = _warehouse_context(parent_warehouse)
        location_ctx = _warehouse_context(location_warehouse)

        assignments.append({
            "parent_warehouse": parent_warehouse,
            "parent_warehouse_name": parent_ctx.get("warehouse_name"),
            "location_warehouse": location_warehouse,
            "location_name": location_ctx.get("warehouse_name"),
            "location_is_child_of_parent": _is_child_warehouse(
                location_warehouse,
                parent_warehouse,
            ),
            "item_group": item_group,
            "valid_from": str(_doc_get(row, "valid_from")) if _doc_get(row, "valid_from") else None,
            "valid_until": str(_doc_get(row, "valid_until")) if _doc_get(row, "valid_until") else None,
        })

    return assignments


def _validate_agent_access_token_doc(token_doc, expected_campaign=None, device_id=None):
    """
    Validate IC-S5-01 token semantics.

    The token authorizes an Inventory Agent to create local mobile sessions
    for an Inventory Campaign. It does not create an ERPNext Inventory Session.
    """

    base_validation = _validate_access_token_doc(
        token_doc,
        expected_campaign=expected_campaign,
    )

    if not base_validation.get("valid"):
        return base_validation

    token_type = _doc_get(token_doc, "token_type") or AGENT_CAMPAIGN_TOKEN_TYPE

    if token_type not in {AGENT_CAMPAIGN_TOKEN_TYPE, LEGACY_CAMPAIGN_TOKEN_TYPE, None}:
        return {
            "valid": False,
            "reason": "Token type is not supported for mobile agent access",
            "event_type": "TOKEN_TYPE_INVALID",
            "status": "Failed",
        }

    inventory_agent = _safe_str(_doc_get(token_doc, "inventory_agent"))

    # Legacy tokens can still be validated by the older endpoint. This endpoint
    # requires Inventory Agent when the field exists in the model.
    if _has_field("Inventory Access Token", "inventory_agent") and not inventory_agent:
        return {
            "valid": False,
            "reason": "Token is not linked to an Inventory Agent",
            "event_type": "TOKEN_AGENT_MISSING",
            "status": "Failed",
        }

    agent_context = _get_inventory_agent_context(inventory_agent)

    if not agent_context:
        return {
            "valid": False,
            "reason": "Inventory Agent does not exist",
            "event_type": "TOKEN_AGENT_INVALID",
            "status": "Failed",
        }

    if agent_context.get("status") != "Active":
        return {
            "valid": False,
            "reason": f"Inventory Agent status is {agent_context.get('status')}",
            "event_type": "TOKEN_AGENT_DISABLED",
            "status": "Failed",
        }

    bind_to_first_device = _safe_bool(_doc_get(token_doc, "bind_to_first_device"), default=False)
    bound_device_id = _safe_str(_doc_get(token_doc, "bound_device_id"))
    device_id = _safe_str(device_id)

    if bind_to_first_device and bound_device_id and device_id and bound_device_id != device_id:
        return {
            "valid": False,
            "reason": "Token is already bound to another device",
            "event_type": "TOKEN_DEVICE_MISMATCH",
            "status": "Failed",
        }

    campaign_context = _get_campaign_context(_doc_get(token_doc, "campaign"))

    if not campaign_context:
        return {
            "valid": False,
            "reason": "Inventory Campaign does not exist",
            "event_type": "TOKEN_CAMPAIGN_INVALID",
            "status": "Failed",
        }

    if campaign_context.get("status") not in {"Open", "Draft"}:
        return {
            "valid": False,
            "reason": f"Inventory Campaign status is {campaign_context.get('status')}",
            "event_type": "TOKEN_CAMPAIGN_NOT_OPEN",
            "status": "Failed",
        }

    assignments = _get_agent_assignments(
        inventory_agent,
        campaign_warehouse=campaign_context.get("warehouse"),
    )

    if not assignments:
        return {
            "valid": False,
            "reason": "Inventory Agent has no active warehouse/location authorization for this campaign",
            "event_type": "TOKEN_AGENT_ASSIGNMENT_MISSING",
            "status": "Failed",
        }

    return {
        "valid": True,
        "reason": None,
        "event_type": "AGENT_ACCESS_TOKEN_VALID",
        "status": "Success",
        "agent_context": agent_context,
        "campaign_context": campaign_context,
        "assignments": assignments,
    }


def _maybe_bind_token_to_device(token_doc, device_id):
    if not token_doc:
        return False

    if not _has_field("Inventory Access Token", "bind_to_first_device"):
        return False

    bind_to_first_device = _safe_bool(_doc_get(token_doc, "bind_to_first_device"), default=False)
    bound_device_id = _safe_str(_doc_get(token_doc, "bound_device_id"))
    device_id = _safe_str(device_id)

    if not bind_to_first_device or bound_device_id or not device_id:
        return False

    frappe.db.set_value(
        "Inventory Access Token",
        token_doc.name,
        {
            "bound_device_id": device_id,
            "bound_at": now_datetime(),
        },
        update_modified=False,
    )
    frappe.db.commit()

    return True


def _touch_access_token(token_doc):
    if not token_doc:
        return

    try:
        frappe.db.set_value(
            "Inventory Access Token",
            token_doc.name,
            "last_used_at",
            now_datetime(),
            update_modified=False,
        )
        frappe.db.commit()
    except Exception:
        frappe.log_error(
            title="Inventory Campaign - token_touch_failed",
            message=frappe.get_traceback(),
        )


def create_mobile_session_token(
    access_token_name,
    campaign,
    inventory_agent,
    device_id=None,
    valid_until=None,
):
    """
    Create a stateless signed mobile token.

    This token is not an ERPNext API key. It is a short-lived signed proof that
    validate_agent_access_token succeeded. Later APIs can verify the signature
    without storing a clear token on the mobile device.
    """

    issued_at = now_datetime()
    expires_at = get_datetime(valid_until) if valid_until else None

    if not expires_at:
        expires_at = add_to_date(issued_at, hours=24, as_datetime=True)

    payload = {
        "typ": "inventory_mobile_session",
        "iat": _iso_datetime(issued_at),
        "exp": _iso_datetime(expires_at),
        "access_token": access_token_name,
        "campaign": campaign,
        "inventory_agent": inventory_agent,
        "device_id": _safe_str(device_id),
    }

    payload_json = frappe.as_json(payload)
    payload_b64 = _base64_urlsafe_encode(payload_json.encode("utf-8"))
    signature = hmac.new(
        _get_server_secret(),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return f"{payload_b64}.{signature}", payload


def verify_mobile_session_token(mobile_session_token):
    """
    Verify a mobile_session_token generated by create_mobile_session_token().

    This helper is intentionally added in IC-S5-01 so IC-S5-04 submit can reuse
    it without reworking the token format.
    """

    mobile_session_token = _safe_str(mobile_session_token)

    if not mobile_session_token or "." not in mobile_session_token:
        return {
            "valid": False,
            "reason": "Mobile session token is missing or malformed",
            "payload": None,
        }

    payload_b64, signature = mobile_session_token.rsplit(".", 1)

    expected_signature = hmac.new(
        _get_server_secret(),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        return {
            "valid": False,
            "reason": "Mobile session token signature is invalid",
            "payload": None,
        }

    try:
        payload = frappe.parse_json(_base64_urlsafe_decode(payload_b64).decode("utf-8"))
    except Exception:
        return {
            "valid": False,
            "reason": "Mobile session token payload is invalid",
            "payload": None,
        }

    expires_at = get_datetime(payload.get("exp")) if payload.get("exp") else None

    if expires_at and now_datetime() > expires_at:
        return {
            "valid": False,
            "reason": "Mobile session token has expired",
            "payload": payload,
        }

    return {
        "valid": True,
        "reason": None,
        "payload": payload,
    }


@frappe.whitelist(allow_guest=True)
def validate_agent_access_token(
    token=None,
    campaign=None,
    device_id=None,
    ssid=None,
    ip_address=None,
):
    """
    Sprint 2 compatibility wrapper.

    The public endpoint path remains:
        inventory_campaign.api.security.validate_agent_access_token

    The implementation now uses the corrected Sprint 1/2 model:
        Inventory Agent.agent_token + Inventory Agent.agent_token_hash

    Legacy Inventory Access Token is intentionally not used here anymore.
    """

    from inventory_campaign.api.agent import validate_agent_access_token as _validate_agent_access_token

    return _validate_agent_access_token(
        token=token,
        campaign=campaign,
        device_id=device_id,
        ssid=ssid,
        ip_address=ip_address,
    )



# ---------------------------------------------------------------------
# Central security enforcement / audit
# ---------------------------------------------------------------------

def enforce_or_audit_security(
    token=None,
    campaign=None,
    session=None,
    device_id=None,
    ssid=None,
    ip_address=None,
    operator_name=None,
    require_access_token=None,
    require_network_check=None,
    request_path=None,
    raise_exception=False,
):
    """
    Central reusable security gate for future business APIs.

    This function is NOT exposed as a whitelisted API.
    It is meant to be called internally by APIs such as:
    - open_inventory_session
    - submit_inventory_session
    - get_campaign_items
    - close_inventory_session

    Original security rules:
    - Disabled:
        Security is inactive. The function allows access without evaluating
        token/network requirements.

    - Audit Only:
        Configured checks are evaluated and logged, but access is allowed.

    - Enforced:
        Configured checks are evaluated and access is denied when a required
        check fails.

    Important:
    - This function does not consume an Inventory Access Token.
    - used_sessions must be incremented later, at the real session-opening step.
    - The clear token is never stored in logs.
    """

    context = get_security_context_dict()
    security_mode = context.get("security_mode")
    security_disabled = bool(context.get("security_disabled"))
    security_audit_only = bool(context.get("security_audit_only"))
    security_blocking = bool(context.get("security_blocking"))

    campaign = _safe_str(campaign)
    session = _safe_str(session)
    device_id = _safe_str(device_id)
    ssid = _safe_str(ssid)
    operator_name = _safe_str(operator_name)
    request_path = _safe_str(request_path) or _get_request_path()

    # Disabled means development/testing bypass.
    # We do not evaluate token or network in this mode.
    if security_disabled:
        log_name = None

        if context.get("log_security_events"):
            log_name = log_security_event(
                event_type="SECURITY_DISABLED",
                status="Allowed",
                security_mode=security_mode,
                campaign=campaign,
                session=session,
                operator_name=operator_name,
                device_id=device_id,
                ip_address=_normalize_ip_address(ip_address),
                ssid=ssid,
                request_path=request_path,
                message="Security mode is Disabled. Access allowed without token/network enforcement.",
                payload={
                    "access_allowed": True,
                    "security_disabled": True,
                },
            )

        return {
            "ok": True,
            "access_allowed": True,
            "security_mode": security_mode,
            "security_disabled": True,
            "security_audit_only": False,
            "security_blocking": False,
            "checked_access_token": False,
            "checked_network": False,
            "token_valid": None,
            "network_valid": None,
            "failures": [],
            "log": log_name,
        }

    # Explicit function parameters can make a specific API stricter than global
    # settings. If omitted, global settings are used.
    token_check_required = (
        bool(context.get("require_access_token"))
        if require_access_token is None
        else bool(require_access_token)
    )

    network_check_required = (
        bool(context.get("require_network_check"))
        if require_network_check is None
        else bool(require_network_check)
    )

    failures = []
    token_result = {
        "checked": False,
        "valid": None,
        "reason": None,
        "event_type": None,
        "access_token": None,
        "campaign": campaign,
        "company": None,
        "warehouse": None,
        "operator_user": None,
        "operator_name": operator_name,
    }
    network_result = {
        "checked": False,
        "valid": None,
        "reason": None,
        "event_type": None,
        "ip_address": _normalize_ip_address(ip_address),
        "ssid": ssid,
        "ip_match": None,
        "ssid_match": None,
    }

    token_doc = None

    if token_check_required:
        token_result["checked"] = True

        token_value = _safe_str(token)

        if not token_value:
            token_result.update({
                "valid": False,
                "reason": "Token is missing",
                "event_type": "TOKEN_MISSING",
            })
        else:
            try:
                token_doc = _find_access_token_doc(token_value)
                validation = _validate_access_token_doc(token_doc, expected_campaign=campaign)

                token_result.update({
                    "valid": bool(validation.get("valid")),
                    "reason": validation.get("reason"),
                    "event_type": validation.get("event_type"),
                })
            except Exception:
                token_result.update({
                    "valid": False,
                    "reason": "Token validation failed because of a server error",
                    "event_type": "TOKEN_VALIDATION_ERROR",
                })

                frappe.log_error(
                    title="Inventory Campaign - enforce_token_validation_failed",
                    message=frappe.get_traceback(),
                )

        if token_doc:
            token_result.update({
                "access_token": token_doc.name,
                "campaign": token_doc.campaign,
                "company": token_doc.company,
                "warehouse": token_doc.warehouse,
                "operator_user": token_doc.operator_user,
                "operator_name": token_doc.operator_name or operator_name,
            })

        if not token_result["valid"]:
            failures.append({
                "check": "access_token",
                "reason": token_result["reason"],
                "event_type": token_result["event_type"],
            })

    if network_check_required:
        network_result["checked"] = True

        try:
            validation = _validate_network_values(ip_address=ip_address, ssid=ssid)

            network_result.update({
                "valid": bool(validation.get("valid")),
                "reason": validation.get("reason"),
                "event_type": validation.get("event_type"),
                "ip_address": validation.get("ip_address"),
                "ssid": validation.get("ssid"),
                "ip_match": validation.get("ip_match"),
                "ssid_match": validation.get("ssid_match"),
                "has_ip_rules": validation.get("has_ip_rules"),
                "has_ssid_rules": validation.get("has_ssid_rules"),
            })
        except Exception:
            network_result.update({
                "valid": False,
                "reason": "Network validation failed because of a server error",
                "event_type": "NETWORK_VALIDATION_ERROR",
                "ip_address": _normalize_ip_address(ip_address),
                "ssid": ssid,
            })

            frappe.log_error(
                title="Inventory Campaign - enforce_network_validation_failed",
                message=frappe.get_traceback(),
            )

        if not network_result["valid"]:
            failures.append({
                "check": "network",
                "reason": network_result["reason"],
                "event_type": network_result["event_type"],
            })

    # Decision:
    # - Audit Only never blocks.
    # - Enforced blocks if at least one required check failed.
    access_allowed = True

    if security_blocking and failures:
        access_allowed = False

    if access_allowed and failures and security_audit_only:
        event_type = "SECURITY_AUDIT_WARNING"
        status = "Warning"
        message = "Security checks failed in Audit Only mode, but access is allowed."
    elif access_allowed:
        event_type = "SECURITY_ACCESS_GRANTED"
        status = "Success"
        message = "Security checks passed. Access allowed."
    else:
        event_type = "SECURITY_ACCESS_DENIED"
        status = "Blocked"
        message = "Security checks failed. Access denied."

    log_name = None

    if context.get("log_security_events"):
        log_name = log_security_event(
            event_type=event_type,
            status=status,
            security_mode=security_mode,
            campaign=token_result.get("campaign") or campaign,
            session=session,
            access_token=token_result.get("access_token"),
            operator_user=token_result.get("operator_user"),
            operator_name=token_result.get("operator_name") or operator_name,
            device_id=device_id,
            ip_address=network_result.get("ip_address"),
            ssid=network_result.get("ssid"),
            request_path=request_path,
            message=message,
            payload={
                "access_allowed": access_allowed,
                "security_audit_only": security_audit_only,
                "security_blocking": security_blocking,
                "token_check_required": token_check_required,
                "network_check_required": network_check_required,
                "token_result": {
                    "checked": token_result.get("checked"),
                    "valid": token_result.get("valid"),
                    "reason": token_result.get("reason"),
                    "event_type": token_result.get("event_type"),
                    "access_token": token_result.get("access_token"),
                    "campaign": token_result.get("campaign"),
                    "company": token_result.get("company"),
                    "warehouse": token_result.get("warehouse"),
                },
                "network_result": {
                    "checked": network_result.get("checked"),
                    "valid": network_result.get("valid"),
                    "reason": network_result.get("reason"),
                    "event_type": network_result.get("event_type"),
                    "ip_address": network_result.get("ip_address"),
                    "ssid": network_result.get("ssid"),
                    "ip_match": network_result.get("ip_match"),
                    "ssid_match": network_result.get("ssid_match"),
                    "has_ip_rules": network_result.get("has_ip_rules"),
                    "has_ssid_rules": network_result.get("has_ssid_rules"),
                },
                "failures": failures,
            },
        )

    result = {
        "ok": True,
        "access_allowed": access_allowed,
        "security_mode": security_mode,
        "security_disabled": False,
        "security_audit_only": security_audit_only,
        "security_blocking": security_blocking,

        "checked_access_token": token_check_required,
        "checked_network": network_check_required,

        "token_valid": token_result.get("valid"),
        "network_valid": network_result.get("valid"),

        "token_result": token_result,
        "network_result": network_result,
        "failures": failures,
        "log": log_name,
    }

    if not access_allowed and raise_exception:
        reason_text = "; ".join(
            failure.get("reason") or failure.get("event_type") or failure.get("check")
            for failure in failures
        )
        frappe.throw(reason_text or "Inventory Campaign security check failed.")

    return result


