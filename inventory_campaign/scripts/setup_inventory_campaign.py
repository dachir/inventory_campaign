# apps/inventory_campaign/inventory_campaign/scripts/setup_inventory_campaign.py

"""One-shot setup for Inventory Campaign data model.

This script remains idempotent across the early development sprints.

Sprint 1 correction:
- Inventory Agent is a business object separate from ERPNext User.
- Authorized scope is explicit: locations and items.
- Unexpected field discoveries are stored as JSON evidence on Inventory Session.
- No Item, Warehouse, Item Group, or Category is auto-created from mobile data.

Sprint 2.5 addition:
- Optional item recoding is captured on Inventory Session Item.
- External agents may propose only Famille and Category.
- Booklet QR payloads use a keyed object format such as
  {"famille":{"code":"HYD","description":"Hydraulique"}}.
- Recoding remains a proposal/evidence layer and never modifies Item master data
  directly from the mobile app.

Sprint 4 addition:
- Submitted Inventory Sessions can be imported into draft Stock Reconciliation.
- Imported sessions are marked as Imported and linked to the reconciliation.
- Stock Reconciliation remains draft; no automatic submission is performed.
"""

import frappe


MODULE = "Inventory Campaign"
APP_NAME = "inventory_campaign"

ROLE_MANAGER = "Inventory Campaign Manager"
ROLE_OPERATOR = "Inventory Campaign Operator"

LEGACY_DOCTYPES = {
    "Inventory Access Token",
    "Inventory Agent Assignment",
}


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------


def execute():
    """
    Create / normalize the corrected Sprint 1 model.

    Safe to re-run:
    - existing DocTypes are not deleted or rebuilt;
    - missing fields are appended;
    - safe metadata such as labels/options/descriptions is normalized;
    - legacy structures are not created on fresh installs, but are tolerated when
      they already exist on a development site.
    """

    frappe.flags.in_setup_inventory_campaign = True

    ensure_module_def()
    ensure_roles()

    create_inventory_campaign_settings()
    create_inventory_security_log()

    create_inventory_agent_authorized_location()
    create_inventory_agent_authorized_item_group()
    create_inventory_agent_authorized_item()  # legacy/manual scope, hidden and ignored by mobile login
    create_inventory_session_location()
    create_inventory_session_item()

    create_inventory_campaign()
    create_inventory_agent()
    create_inventory_session()

    create_stock_reconciliation_custom_fields()
    normalize_inventory_campaign_modules()
    mark_legacy_doctypes_if_present()

    frappe.db.commit()
    frappe.clear_cache()

    print("Inventory Campaign Sprint 4 Stock Reconciliation import setup completed.")


# -----------------------------------------------------------------------------
# Module and roles
# -----------------------------------------------------------------------------


def ensure_module_def():
    if frappe.db.exists("Module Def", MODULE):
        print(f"Module already exists: {MODULE}")
        return

    module = frappe.get_doc({
        "doctype": "Module Def",
        "module_name": MODULE,
        "app_name": APP_NAME,
        "custom": 1,
    })
    module.insert(ignore_permissions=True)
    print(f"Created Module Def: {MODULE}")



def ensure_roles():
    ensure_role(ROLE_MANAGER, desk_access=1)
    ensure_role(ROLE_OPERATOR, desk_access=0)



def ensure_role(role_name, desk_access=0):
    if frappe.db.exists("Role", role_name):
        print(f"Role already exists: {role_name}")
        return

    role = frappe.get_doc({
        "doctype": "Role",
        "role_name": role_name,
        "desk_access": desk_access,
    })
    role.insert(ignore_permissions=True)
    print(f"Created Role: {role_name}")


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------


def ensure_doctype(
    name,
    fields,
    permissions=None,
    autoname=None,
    title_field=None,
    istable=0,
    is_submittable=0,
    issingle=0,
):
    """Create a custom DocType or append missing fields if it already exists."""

    if frappe.db.exists("DocType", name):
        normalize_doctype_header(
            name,
            autoname=autoname,
            title_field=title_field,
            istable=istable,
            issingle=issingle,
            is_submittable=is_submittable,
        )
        ensure_doctype_fields(name, fields)
        frappe.clear_cache(doctype=name)
        print(f"DocType already exists and was normalized: {name}")
        return

    doc = frappe.get_doc({
        "doctype": "DocType",
        "name": name,
        "module": MODULE,
        "custom": 1,
        "istable": istable,
        "issingle": issingle,
        "editable_grid": 1 if istable else 0,
        "is_submittable": is_submittable,
        "autoname": autoname,
        "title_field": title_field,
        "track_changes": 1,
        "allow_rename": 0,
        "sort_field": "modified",
        "sort_order": "DESC",
        "fields": fields,
        "permissions": permissions or [],
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    frappe.clear_cache(doctype=name)
    print(f"Created DocType: {name}")



def normalize_doctype_header(
    doctype_name,
    autoname=None,
    title_field=None,
    istable=0,
    issingle=0,
    is_submittable=0,
):
    values = {
        "module": MODULE,
        "custom": 1,
        "istable": istable,
        "issingle": issingle,
        "editable_grid": 1 if istable else 0,
        "is_submittable": is_submittable,
    }

    if autoname:
        values["autoname"] = autoname
    if title_field:
        values["title_field"] = title_field

    for fieldname, value in values.items():
        try:
            frappe.db.set_value(
                "DocType",
                doctype_name,
                fieldname,
                value,
                update_modified=False,
            )
        except Exception:
            # Some properties may be protected depending on the Frappe version.
            # The field-level normalization remains the important part.
            pass



def ensure_doctype_fields(doctype_name, fields):
    if not frappe.db.exists("DocType", doctype_name):
        print(f"Cannot ensure fields. DocType missing: {doctype_name}")
        return

    doctype = frappe.get_doc("DocType", doctype_name)
    changed = False

    for field in fields:
        fieldname = field.get("fieldname")
        if not fieldname:
            continue

        existing_field_name = frappe.db.exists(
            "DocField",
            {
                "parent": doctype_name,
                "parenttype": "DocType",
                "fieldname": fieldname,
            },
        )

        if existing_field_name:
            normalize_docfield_properties(existing_field_name, field)
            print(f"DocField already exists: {doctype_name}.{fieldname}")
            continue

        doctype.append("fields", field)
        changed = True
        print(f"Added DocField: {doctype_name}.{fieldname}")

    if changed:
        doctype.save(ignore_permissions=True)
        frappe.db.commit()

    frappe.clear_cache(doctype=doctype_name)



def normalize_docfield_properties(docfield_name, field):
    """Update safe DocField properties only; never drop data or alter fieldtype."""

    safe_properties = [
        "label",
        "options",
        "default",
        "reqd",
        "read_only",
        "hidden",
        "in_list_view",
        "in_standard_filter",
        "description",
        "depends_on",
        "collapsible",
        "unique",
        "insert_after",
    ]

    for prop in safe_properties:
        if prop in field:
            frappe.db.set_value(
                "DocField",
                docfield_name,
                prop,
                field.get(prop),
                update_modified=False,
            )



def create_custom_field(dt, field):
    fieldname = field.get("fieldname")
    if not fieldname:
        raise ValueError("Custom Field must have a fieldname.")

    custom_field_name = f"{dt}-{fieldname}"

    if frappe.db.exists("Custom Field", custom_field_name):
        normalize_custom_field(custom_field_name, field)
        print(f"Custom Field already exists and was normalized: {custom_field_name}")
        return

    custom_field_data = {
        "doctype": "Custom Field",
        "dt": dt,
        **field,
    }

    if custom_field_has_module():
        custom_field_data["module"] = MODULE

    doc = frappe.get_doc(custom_field_data)
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    frappe.clear_cache(doctype=dt)
    print(f"Created Custom Field: {custom_field_name}")



def normalize_custom_field(custom_field_name, field):
    safe_properties = [
        "label",
        "fieldtype",
        "options",
        "default",
        "insert_after",
        "depends_on",
        "read_only",
        "description",
        "collapsible",
    ]

    for prop in safe_properties:
        if prop in field:
            frappe.db.set_value(
                "Custom Field",
                custom_field_name,
                prop,
                field.get(prop),
                update_modified=False,
            )

    normalize_custom_field_module(custom_field_name)



def custom_field_has_module():
    try:
        return frappe.get_meta("Custom Field").has_field("module")
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Permission helpers
# -----------------------------------------------------------------------------


def full_perm(role):
    return {
        "role": role,
        "read": 1,
        "write": 1,
        "create": 1,
        "delete": 1,
        "submit": 0,
        "cancel": 0,
        "amend": 0,
        "report": 1,
        "export": 1,
        "import": 0,
        "share": 1,
        "print": 1,
        "email": 1,
    }



def operator_session_perm(role):
    return {
        "role": role,
        "read": 1,
        "write": 1,
        "create": 1,
        "delete": 0,
        "submit": 0,
        "cancel": 0,
        "amend": 0,
        "report": 0,
        "export": 0,
        "import": 0,
        "share": 0,
        "print": 0,
        "email": 0,
    }



def read_only_perm(role):
    return {
        "role": role,
        "read": 1,
        "write": 0,
        "create": 0,
        "delete": 0,
        "submit": 0,
        "cancel": 0,
        "amend": 0,
        "report": 1,
        "export": 0,
        "import": 0,
        "share": 0,
        "print": 1,
        "email": 0,
    }



def security_log_perm(role):
    data = full_perm(role)
    data["delete"] = 0
    data["share"] = 0
    return data



def security_log_read_only_perm(role):
    data = read_only_perm(role)
    data["export"] = 1
    return data


# -----------------------------------------------------------------------------
# Single DocType: Inventory Campaign Settings
# -----------------------------------------------------------------------------


def create_inventory_campaign_settings():
    fields = [
        {
            "fieldname": "security_section",
            "label": "Security Mode",
            "fieldtype": "Section Break",
        },
        {
            "fieldname": "security_mode",
            "label": "Security Mode",
            "fieldtype": "Select",
            "options": "Disabled\nAudit Only\nEnforced",
            "default": "Disabled",
            "reqd": 1,
            "description": "Disabled during early development/testing. Enforced only in final hardening.",
        },
        {
            "fieldname": "require_agent_token",
            "label": "Require Agent Token",
            "fieldtype": "Check",
            "default": "0",
            "description": "Uses the one-token-per-agent model. Keep disabled during early development/testing.",
        },
        {
            "fieldname": "require_access_token",
            "label": "Require Access Token (Legacy Alias)",
            "fieldtype": "Check",
            "default": "0",
            "description": "Backward-compatible alias for older security API code. Do not use as the Sprint 1 business model.",
        },
        {
            "fieldname": "require_network_check",
            "label": "Require Network Check",
            "fieldtype": "Check",
            "default": "0",
            "description": "Keep disabled during early development/testing.",
        },
        {
            "fieldname": "security_column",
            "fieldtype": "Column Break",
        },
        {
            "fieldname": "allow_development_bypass",
            "label": "Allow Development Bypass",
            "fieldtype": "Check",
            "default": "1",
        },
        {
            "fieldname": "log_security_events",
            "label": "Log Security Events",
            "fieldtype": "Check",
            "default": "1",
        },
        {
            "fieldname": "mobile_credential_section",
            "label": "Mobile Credential Defaults",
            "fieldtype": "Section Break",
        },
        {
            "fieldname": "default_mobile_credential_ttl_minutes",
            "label": "Default Mobile Credential TTL Minutes",
            "fieldtype": "Int",
            "default": "480",
            "description": "Default temporary credential duration after validating an agent token.",
        },
        {
            "fieldname": "network_section",
            "label": "Network Defaults",
            "fieldtype": "Section Break",
        },
        {
            "fieldname": "allowed_ssids",
            "label": "Allowed SSIDs",
            "fieldtype": "Small Text",
            "description": "One SSID per line. SSID is weak and must not be the only proof.",
        },
        {
            "fieldname": "allowed_ip_ranges",
            "label": "Allowed IP Ranges",
            "fieldtype": "Small Text",
            "description": "One CIDR or IP range per line.",
        },
        {
            "fieldname": "server_reachable_url",
            "label": "Server Reachable URL",
            "fieldtype": "Data",
        },
        {
            "fieldname": "notes_section",
            "label": "Notes",
            "fieldtype": "Section Break",
        },
        {
            "fieldname": "notes",
            "label": "Notes",
            "fieldtype": "Small Text",
        },
    ]

    ensure_doctype(
        name="Inventory Campaign Settings",
        fields=fields,
        permissions=[
            full_perm("System Manager"),
            full_perm("Stock Manager"),
            full_perm(ROLE_MANAGER),
        ],
        issingle=1,
    )
    ensure_inventory_campaign_settings_defaults()



def ensure_inventory_campaign_settings_defaults():
    if not frappe.db.exists("DocType", "Inventory Campaign Settings"):
        return

    defaults = {
        "security_mode": "Disabled",
        "require_agent_token": 0,
        "require_access_token": 0,
        "require_network_check": 0,
        "allow_development_bypass": 1,
        "log_security_events": 1,
        "default_mobile_credential_ttl_minutes": 480,
    }

    for fieldname, value in defaults.items():
        try:
            current_value = frappe.db.get_single_value("Inventory Campaign Settings", fieldname)
            if current_value in (None, ""):
                frappe.db.set_single_value("Inventory Campaign Settings", fieldname, value)
        except Exception:
            pass

    frappe.clear_cache(doctype="Inventory Campaign Settings")


# -----------------------------------------------------------------------------
# DocType: Inventory Security Log
# -----------------------------------------------------------------------------


def create_inventory_security_log():
    fields = [
        {"fieldname": "event_section", "label": "Security Event", "fieldtype": "Section Break"},
        {
            "fieldname": "event_type",
            "label": "Event Type",
            "fieldtype": "Data",
            "reqd": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
        },
        {
            "fieldname": "status",
            "label": "Status",
            "fieldtype": "Select",
            "options": "Success\nFailed\nWarning\nBlocked\nAllowed",
            "default": "Success",
            "reqd": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
        },
        {
            "fieldname": "security_mode",
            "label": "Security Mode",
            "fieldtype": "Select",
            "options": "Disabled\nAudit Only\nEnforced",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "event_time",
            "label": "Event Time",
            "fieldtype": "Datetime",
            "read_only": 1,
            "in_list_view": 1,
        },
        {"fieldname": "context_section", "label": "Inventory Context", "fieldtype": "Section Break"},
        {
            "fieldname": "campaign",
            "label": "Inventory Campaign",
            "fieldtype": "Link",
            "options": "Inventory Campaign",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "session",
            "label": "Inventory Session",
            "fieldtype": "Link",
            "options": "Inventory Session",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "mobile_session_id",
            "label": "Mobile Session ID",
            "fieldtype": "Data",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "inventory_agent",
            "label": "Inventory Agent",
            "fieldtype": "Link",
            "options": "Inventory Agent",
            "in_standard_filter": 1,
        },
        {"fieldname": "request_column", "fieldtype": "Column Break"},
        {
            "fieldname": "device_id",
            "label": "Device ID",
            "fieldtype": "Data",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "ip_address",
            "label": "IP Address",
            "fieldtype": "Data",
            "in_standard_filter": 1,
        },
        {"fieldname": "ssid", "label": "SSID", "fieldtype": "Data", "in_standard_filter": 1},
        {"fieldname": "request_path", "label": "Request Path", "fieldtype": "Data"},
        {"fieldname": "message_section", "label": "Message", "fieldtype": "Section Break"},
        {"fieldname": "message", "label": "Message", "fieldtype": "Small Text"},
        {
            "fieldname": "payload_json",
            "label": "Payload JSON",
            "fieldtype": "Code",
            "options": "JSON",
            "read_only": 1,
            "description": "Never store clear tokens or sensitive secrets here.",
        },
    ]

    ensure_doctype(
        name="Inventory Security Log",
        fields=fields,
        permissions=[
            security_log_perm("System Manager"),
            security_log_perm("Stock Manager"),
            security_log_perm(ROLE_MANAGER),
            security_log_read_only_perm("Stock User"),
        ],
        autoname="format:ISL-.YYYY.-.#####",
        title_field="event_type",
    )


# -----------------------------------------------------------------------------
# Child doctypes
# -----------------------------------------------------------------------------


def create_inventory_agent_authorized_location():
    fields = [
        {"fieldname": "location_warehouse", "label": "Location / Emplacement", "fieldtype": "Link", "options": "Warehouse", "reqd": 1, "in_list_view": 1},
        {"fieldname": "parent_warehouse", "label": "Parent Warehouse", "fieldtype": "Link", "options": "Warehouse", "in_standard_filter": 1},
        {"fieldname": "active", "label": "Active", "fieldtype": "Check", "default": "1", "in_list_view": 1},
    ]
    ensure_doctype(
        name="Inventory Agent Authorized Location",
        fields=fields,
        permissions=[],
        autoname="hash",
        istable=1,
    )




def create_inventory_agent_authorized_item_group():
    fields = [
        {
            "fieldname": "item_group",
            "label": "Item Group",
            "fieldtype": "Link",
            "options": "Item Group",
            "reqd": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
        },
    ]
    ensure_doctype(
        name="Inventory Agent Authorized Item Group",
        fields=fields,
        permissions=[],
        autoname="hash",
        istable=1,
    )


def create_inventory_agent_authorized_item():
    fields = [
        {"fieldname": "item_code", "label": "Item", "fieldtype": "Link", "options": "Item", "reqd": 1, "in_list_view": 1},
        {"fieldname": "item_name", "label": "Item Name", "fieldtype": "Data", "read_only": 1, "in_list_view": 1},
        {"fieldname": "active", "label": "Active", "fieldtype": "Check", "default": "1", "in_list_view": 1},
    ]
    ensure_doctype(
        name="Inventory Agent Authorized Item",
        fields=fields,
        permissions=[],
        autoname="hash",
        istable=1,
    )



def create_inventory_session_location():
    fields = [
        {"fieldname": "location_section", "label": "Location", "fieldtype": "Section Break"},
        {
            "fieldname": "parent_warehouse",
            "label": "Parent Warehouse",
            "fieldtype": "Link",
            "options": "Warehouse",
            "reqd": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
        },
        {
            "fieldname": "location_warehouse",
            "label": "Location / Emplacement",
            "fieldtype": "Link",
            "options": "Warehouse",
            "reqd": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
            "description": "Child Warehouse used as physical location/emplacement.",
        },
        {"fieldname": "location_name", "label": "Location Name", "fieldtype": "Data", "read_only": 1, "in_list_view": 1},
        {"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
    ]
    ensure_doctype(
        name="Inventory Session Location",
        fields=fields,
        permissions=[],
        autoname="hash",
        istable=1,
    )



def create_inventory_session_item():
    fields = [
        {"fieldname": "item_section", "label": "Item", "fieldtype": "Section Break"},
        {
            "fieldname": "item_code",
            "label": "Item Code",
            "fieldtype": "Link",
            "options": "Item",
            "reqd": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
        },
        {"fieldname": "item_name", "label": "Item Name", "fieldtype": "Data", "read_only": 1, "in_list_view": 1},
        {"fieldname": "barcode", "label": "Barcode / QR Code", "fieldtype": "Data", "in_list_view": 1, "in_standard_filter": 1},
        {"fieldname": "uom", "label": "UOM", "fieldtype": "Link", "options": "UOM", "in_list_view": 1},
        {"fieldname": "count_section", "label": "Count", "fieldtype": "Section Break"},
        {"fieldname": "counted_qty", "label": "Counted Quantity", "fieldtype": "Float", "reqd": 1, "in_list_view": 1},
        {"fieldname": "scan_count", "label": "Scan Count", "fieldtype": "Int", "default": "1", "read_only": 1, "in_list_view": 1},
        {"fieldname": "last_scanned_at", "label": "Last Scanned At", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "manual_entry", "label": "Manual Entry", "fieldtype": "Check", "default": "0"},
        {"fieldname": "location_section", "label": "Location / Emplacement", "fieldtype": "Section Break"},
        {"fieldname": "parent_warehouse", "label": "Parent Warehouse", "fieldtype": "Link", "options": "Warehouse", "in_standard_filter": 1},
        {
            "fieldname": "location_warehouse",
            "label": "Location / Emplacement",
            "fieldtype": "Link",
            "options": "Warehouse",
            "in_list_view": 1,
            "in_standard_filter": 1,
            "description": "Child Warehouse / physical emplacement where the item was counted.",
        },
        {"fieldname": "mobile_line_id", "label": "Mobile Line ID", "fieldtype": "Data"},
        {"fieldname": "recoding_section", "label": "Optional Recoding Proposal", "fieldtype": "Section Break", "collapsible": 1},
        {
            "fieldname": "recoding_required",
            "label": "Recoding Proposed",
            "fieldtype": "Check",
            "default": "0",
            "description": "Checked when the mobile agent proposes a controlled Famille/Category recoding for this counted item.",
        },
        {
            "fieldname": "recoding_status",
            "label": "Recoding Status",
            "fieldtype": "Select",
            "options": "Not Required\nPending Review\nApproved\nRejected\nApplied",
            "default": "Not Required",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "recoding_tags_json",
            "label": "Recoding Tags JSON",
            "fieldtype": "Code",
            "options": "JSON",
            "description": "Optional recoding proposal from booklet QR tags. External agents may freeze only Famille and Category tags. Example: {\"famille\":{\"code\":\"HYD\",\"description\":\"Hydraulique\"}}.",
        },
        {"fieldname": "recoding_summary_column", "fieldtype": "Column Break"},
        {"fieldname": "recoding_famille_code", "label": "Famille Code", "fieldtype": "Data", "read_only": 1, "in_standard_filter": 1},
        {"fieldname": "recoding_famille_description", "label": "Famille", "fieldtype": "Data", "read_only": 1},
        {"fieldname": "recoding_category_code", "label": "Category Code", "fieldtype": "Data", "read_only": 1, "in_standard_filter": 1},
        {"fieldname": "recoding_category_description", "label": "Category", "fieldtype": "Data", "read_only": 1},
        {"fieldname": "recoding_note", "label": "Recoding Note", "fieldtype": "Small Text"},
        {"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
    ]
    ensure_doctype(
        name="Inventory Session Item",
        fields=fields,
        permissions=[],
        autoname="hash",
        istable=1,
    )


# -----------------------------------------------------------------------------
# Main doctypes
# -----------------------------------------------------------------------------


def create_inventory_campaign():
    fields = [
        {"fieldname": "campaign_section", "label": "Campaign", "fieldtype": "Section Break"},
        {"fieldname": "campaign_name", "label": "Campaign Name", "fieldtype": "Data", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
        {"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
        {"fieldname": "warehouse", "label": "Parent Warehouse", "fieldtype": "Link", "options": "Warehouse", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1, "description": "Main stock site. Child Warehouses represent locations/emplacements."},
        {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "Draft\nOpen\nClosed\nCancelled", "default": "Draft", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
        {"fieldname": "date_column", "fieldtype": "Column Break"},
        {"fieldname": "start_date", "label": "Start Date", "fieldtype": "Date", "reqd": 1, "in_list_view": 1},
        {"fieldname": "end_date", "label": "End Date", "fieldtype": "Date", "in_list_view": 1},
        {"fieldname": "responsible_user", "label": "Responsible User", "fieldtype": "Link", "options": "User"},
        {"fieldname": "site_section", "label": "ERPNext Site", "fieldtype": "Section Break", "collapsible": 1},
        {"fieldname": "erpnext_site", "label": "ERPNext Site", "fieldtype": "Data", "default": "erpv15dev.marsavco.com", "description": "Logical ERPNext site/tenant for this campaign."},
        {"fieldname": "site_url", "label": "Site URL", "fieldtype": "Data"},
        {"fieldname": "security_section", "label": "Access Security", "fieldtype": "Section Break", "collapsible": 1},
        {"fieldname": "allowed_network_ssid", "label": "Allowed Network SSID", "fieldtype": "Data"},
        {"fieldname": "allowed_ip_range", "label": "Allowed IP Range", "fieldtype": "Small Text"},
        {"fieldname": "server_reachable_url", "label": "Server Reachable URL", "fieldtype": "Data"},
        {"fieldname": "summary_section", "label": "Summary", "fieldtype": "Section Break"},
        {"fieldname": "total_sessions", "label": "Total Sessions", "fieldtype": "Int", "read_only": 1, "default": "0"},
        {"fieldname": "total_items_counted", "label": "Total Items Counted", "fieldtype": "Int", "read_only": 1, "default": "0"},
        {"fieldname": "total_unplanned_items", "label": "Total Unplanned Items", "fieldtype": "Int", "read_only": 1, "default": "0"},
        {"fieldname": "total_unplanned_warehouses", "label": "Total Unplanned Warehouses", "fieldtype": "Int", "read_only": 1, "default": "0"},
        {"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
    ]
    ensure_doctype(
        name="Inventory Campaign",
        fields=fields,
        permissions=[full_perm("System Manager"), full_perm("Stock Manager"), full_perm(ROLE_MANAGER), read_only_perm("Stock User")],
        autoname="format:IC-.YYYY.-.#####",
        title_field="campaign_name",
    )



def create_inventory_agent():
    fields = [
        {"fieldname": "agent_section", "label": "Agent", "fieldtype": "Section Break"},
        {"fieldname": "agent_code", "label": "Agent Code", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1, "in_standard_filter": 1},
        {"fieldname": "agent_name", "label": "Agent Name", "fieldtype": "Data", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
        {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "Active\nDisabled\nSuspended", "default": "Active", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
        {"fieldname": "contact_column", "fieldtype": "Column Break"},
        {"fieldname": "phone", "label": "Phone", "fieldtype": "Data"},
        {"fieldname": "email", "label": "Email", "fieldtype": "Data"},
        {"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company", "in_standard_filter": 1},
        {"fieldname": "authorization_section", "label": "Authorized Scope", "fieldtype": "Section Break"},
        {
            "fieldname": "authorized_locations",
            "label": "Authorized Locations / Emplacements",
            "fieldtype": "Table MultiSelect",
            "options": "Inventory Agent Authorized Location",
            "description": "Allowed child Warehouses/emplacements for this agent. No unplanned Warehouse is created automatically.",
        },
        {
            "fieldname": "authorized_item_groups",
            "label": "Authorized Item Groups",
            "fieldtype": "Table MultiSelect",
            "options": "Inventory Agent Authorized Item Group",
            "description": "Only active stock Items belonging to these Item Groups are downloaded to the mobile app. Parent Item Groups include their child groups.",
        },
        {
            "fieldname": "authorized_items",
            "label": "Authorized Items (Legacy - Ignored)",
            "fieldtype": "Table MultiSelect",
            "options": "Inventory Agent Authorized Item",
            "hidden": 1,
            "description": "Legacy/manual item scope. Mobile login now derives authorized_items only from Authorized Item Groups.",
        },
        {"fieldname": "token_section", "label": "Agent Token (Dormant Until Security Sprint)", "fieldtype": "Section Break", "collapsible": 1},
        {"fieldname": "agent_token", "label": "Agent Token", "fieldtype": "Password", "description": "One clear root token per Inventory Agent. Used later to generate a temporary mobile credential."},
        {"fieldname": "agent_token_hash", "label": "Agent Token Hash", "fieldtype": "Data", "read_only": 1, "unique": 1},
        {"fieldname": "token_status", "label": "Token Status", "fieldtype": "Select", "options": "Not Generated\nActive\nConsumed\nDisabled\nExpired\nRevoked", "default": "Not Generated", "in_standard_filter": 1},
        {"fieldname": "token_valid_from", "label": "Token Valid From", "fieldtype": "Datetime"},
        {"fieldname": "token_valid_until", "label": "Token Valid Until", "fieldtype": "Datetime"},
        {"fieldname": "token_consumed_at", "label": "Token Consumed At", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "token_consumed_by_device", "label": "Token Consumed By Device", "fieldtype": "Data", "read_only": 1},
        {"fieldname": "mobile_credential_ttl_minutes", "label": "Mobile Credential TTL Minutes", "fieldtype": "Int", "default": "480"},
        {"fieldname": "credential_issued_at", "label": "Credential Issued At", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "credential_expires_at", "label": "Credential Expires At", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "device_section", "label": "Device Binding", "fieldtype": "Section Break", "collapsible": 1},
        {"fieldname": "bind_to_first_device", "label": "Bind To First Device", "fieldtype": "Check", "default": "0"},
        {"fieldname": "bound_device_id", "label": "Bound Device ID", "fieldtype": "Data", "read_only": 1},
        {"fieldname": "bound_at", "label": "Bound At", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "last_token_used_at", "label": "Last Token Used At", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "notes_section", "label": "Notes", "fieldtype": "Section Break"},
        {"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
    ]
    ensure_doctype(
        name="Inventory Agent",
        fields=fields,
        permissions=[full_perm("System Manager"), full_perm("Stock Manager"), full_perm(ROLE_MANAGER), read_only_perm("Stock User")],
        autoname="format:IAG-.YYYY.-.#####",
        title_field="agent_name",
    )



def create_inventory_session():
    fields = [
        {"fieldname": "session_section", "label": "Session", "fieldtype": "Section Break"},
        {"fieldname": "campaign", "label": "Inventory Campaign", "fieldtype": "Link", "options": "Inventory Campaign", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
        {"fieldname": "mobile_session_id", "label": "Mobile Session ID", "fieldtype": "Data", "unique": 1, "in_list_view": 1, "in_standard_filter": 1, "description": "Unique ID generated by the mobile app. Used for idempotent submit."},
        {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "Submitted\nImported\nRejected\nCancelled\nFailed", "default": "Submitted", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
        {"fieldname": "operator_column", "fieldtype": "Column Break"},
        {"fieldname": "inventory_agent", "label": "Inventory Agent", "fieldtype": "Link", "options": "Inventory Agent", "in_list_view": 1, "in_standard_filter": 1, "description": "Operational inventory agent. This is not an ERPNext User."},
        {"fieldname": "operator_user", "label": "Operator User", "fieldtype": "Link", "options": "User", "in_standard_filter": 1, "description": "Optional ERPNext user for manual/back-office trace only."},
        {"fieldname": "operator_name", "label": "Operator Name", "fieldtype": "Data", "in_list_view": 1},
        {"fieldname": "device_id", "label": "Device ID", "fieldtype": "Data", "in_standard_filter": 1},
        {"fieldname": "scope_section", "label": "Session Scope", "fieldtype": "Section Break"},
        {"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company", "reqd": 1, "in_standard_filter": 1},
        {"fieldname": "warehouse", "label": "Warehouse", "fieldtype": "Link", "options": "Warehouse", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1, "description": "Compatibility field; normally same as parent_warehouse."},
        {"fieldname": "parent_warehouse", "label": "Parent Warehouse", "fieldtype": "Link", "options": "Warehouse", "in_standard_filter": 1, "description": "Main warehouse / stock site."},
        {"fieldname": "location_warehouse", "label": "Location / Emplacement", "fieldtype": "Link", "options": "Warehouse", "in_list_view": 1, "in_standard_filter": 1, "description": "Main child Warehouse/emplacement used by the session when there is one primary location."},
        {"fieldname": "locations", "label": "Locations", "fieldtype": "Table", "options": "Inventory Session Location", "description": "Structured planned locations covered by this submitted session."},
        {"fieldname": "zone", "label": "Zone", "fieldtype": "Data", "description": "Optional physical zone, aisle, shelf range, or counting area."},
        {"fieldname": "item_group", "label": "Item Group", "fieldtype": "Link", "options": "Item Group"},
        {"fieldname": "timing_section", "label": "Timing", "fieldtype": "Section Break"},
        {"fieldname": "opened_at", "label": "Opened At", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "closed_at", "label": "Closed At", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "submitted_at", "label": "Submitted At", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "server_ack_at", "label": "Server ACK At", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "submitted_from_mobile", "label": "Submitted From Mobile", "fieldtype": "Check", "default": "1"},
        {"fieldname": "submit_retry_count", "label": "Submit Retry Count", "fieldtype": "Int", "default": "0", "read_only": 1},
        {"fieldname": "items_section", "label": "Counted Items", "fieldtype": "Section Break"},
        {"fieldname": "items", "label": "Items", "fieldtype": "Table", "options": "Inventory Session Item"},
        {"fieldname": "unplanned_section", "label": "Unplanned Field Discoveries", "fieldtype": "Section Break", "collapsible": 1},
        {
            "fieldname": "unplanned_items_json",
            "label": "Unplanned Items JSON",
            "fieldtype": "Code",
            "options": "JSON",
            "read_only": 1,
            "description": "JSON evidence for items found on field but not authorized/planned. It must not auto-create Item or Item Group.",
        },
        {
            "fieldname": "unplanned_warehouses_json",
            "label": "Unplanned Warehouses JSON",
            "fieldtype": "Code",
            "options": "JSON",
            "read_only": 1,
            "description": "JSON evidence for locations/warehouses found on field but not authorized/planned. It must not auto-create Warehouse.",
        },
        {"fieldname": "unplanned_summary_column", "fieldtype": "Column Break"},
        {"fieldname": "has_unplanned_items", "label": "Has Unplanned Items", "fieldtype": "Check", "read_only": 1, "default": "0", "in_standard_filter": 1},
        {"fieldname": "unplanned_items_count", "label": "Unplanned Items Count", "fieldtype": "Int", "read_only": 1, "default": "0"},
        {"fieldname": "has_unplanned_warehouses", "label": "Has Unplanned Warehouses", "fieldtype": "Check", "read_only": 1, "default": "0", "in_standard_filter": 1},
        {"fieldname": "unplanned_warehouses_count", "label": "Unplanned Warehouses Count", "fieldtype": "Int", "read_only": 1, "default": "0"},
        {"fieldname": "recoding_summary_section", "label": "Recoding Summary", "fieldtype": "Section Break", "collapsible": 1},
        {"fieldname": "has_recoding_proposals", "label": "Has Recoding Proposals", "fieldtype": "Check", "read_only": 1, "default": "0", "in_standard_filter": 1},
        {"fieldname": "recoding_proposals_count", "label": "Recoding Proposals Count", "fieldtype": "Int", "read_only": 1, "default": "0"},
        {"fieldname": "review_section", "label": "Supervisor Review", "fieldtype": "Section Break"},
        {"fieldname": "review_status", "label": "Review Status", "fieldtype": "Select", "options": "Pending\nReviewed\nApproved\nRejected\nImported", "default": "Pending", "in_list_view": 1, "in_standard_filter": 1},
        {"fieldname": "reviewed_by", "label": "Reviewed By", "fieldtype": "Link", "options": "User", "read_only": 1},
        {"fieldname": "reviewed_at", "label": "Reviewed At", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "review_note", "label": "Review Note", "fieldtype": "Small Text"},
        {"fieldname": "summary_section", "label": "Summary", "fieldtype": "Section Break"},
        {"fieldname": "total_items_counted", "label": "Total Items Counted", "fieldtype": "Int", "read_only": 1, "default": "0"},
        {"fieldname": "total_qty_counted", "label": "Total Quantity Counted", "fieldtype": "Float", "read_only": 1, "default": "0"},
        {"fieldname": "import_section", "label": "Stock Reconciliation Import", "fieldtype": "Section Break"},
        {"fieldname": "imported_stock_reconciliation", "label": "Imported Stock Reconciliation", "fieldtype": "Link", "options": "Stock Reconciliation", "read_only": 1},
        {"fieldname": "imported_at", "label": "Imported At", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "imported_by", "label": "Imported By", "fieldtype": "Link", "options": "User", "read_only": 1},
        {"fieldname": "technical_section", "label": "Technical", "fieldtype": "Section Break", "collapsible": 1},
        {"fieldname": "raw_payload_json", "label": "Raw Payload JSON", "fieldtype": "Code", "options": "JSON", "read_only": 1},
        {"fieldname": "submit_payload_hash", "label": "Submit Payload Hash", "fieldtype": "Data", "read_only": 1, "in_standard_filter": 1, "description": "SHA-256 hash of the sanitized mobile submit payload. Used with mobile_session_id for safe idempotent ACK handling."},
        {"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
    ]
    ensure_doctype(
        name="Inventory Session",
        fields=fields,
        permissions=[full_perm("System Manager"), full_perm("Stock Manager"), full_perm(ROLE_MANAGER), operator_session_perm(ROLE_OPERATOR), read_only_perm("Stock User")],
        autoname="format:IS-.YYYY.-.#####",
        title_field="mobile_session_id",
    )


# -----------------------------------------------------------------------------
# Stock Reconciliation custom fields
# -----------------------------------------------------------------------------


def create_stock_reconciliation_custom_fields():
    custom_fields = [
        {
            "fieldname": "custom_inventory_campaign_section",
            "label": "Inventory Campaign",
            "fieldtype": "Section Break",
            "insert_after": "company",
            "collapsible": 1,
        },
        {
            "fieldname": "custom_inventory_source",
            "label": "Inventory Source",
            "fieldtype": "Select",
            "options": "Manual\nInventory Campaign",
            "default": "Manual",
            "insert_after": "custom_inventory_campaign_section",
        },
        {
            "fieldname": "custom_inventory_campaign",
            "label": "Inventory Campaign",
            "fieldtype": "Link",
            "options": "Inventory Campaign",
            "insert_after": "custom_inventory_source",
            "depends_on": "eval:doc.custom_inventory_source=='Inventory Campaign'",
        },
        {
            "fieldname": "custom_inventory_session_refs",
            "label": "Inventory Session References",
            "fieldtype": "Long Text",
            "read_only": 1,
            "insert_after": "custom_inventory_campaign",
            "depends_on": "eval:doc.custom_inventory_source=='Inventory Campaign'",
            "description": "Technical trace of imported Inventory Sessions. Filled by Sprint 4 controlled import logic.",
        },
    ]

    for field in custom_fields:
        create_custom_field("Stock Reconciliation", field)

    frappe.clear_cache(doctype="Stock Reconciliation")


# -----------------------------------------------------------------------------
# Normalization and legacy handling
# -----------------------------------------------------------------------------


def normalize_inventory_campaign_modules():
    doctypes = [
        "Inventory Campaign",
        "Inventory Session",
        "Inventory Session Item",
        "Inventory Session Location",
        "Inventory Agent",
        "Inventory Agent Authorized Location",
        "Inventory Agent Authorized Item Group",
        "Inventory Agent Authorized Item",
        "Inventory Campaign Settings",
        "Inventory Security Log",
    ]

    for legacy_doctype in LEGACY_DOCTYPES:
        if frappe.db.exists("DocType", legacy_doctype):
            doctypes.append(legacy_doctype)

    for doctype in doctypes:
        if frappe.db.exists("DocType", doctype):
            current_module = frappe.db.get_value("DocType", doctype, "module")
            if current_module != MODULE:
                frappe.db.set_value("DocType", doctype, "module", MODULE, update_modified=False)
                print(f"Normalized DocType module: {doctype} -> {MODULE}")
            else:
                print(f"DocType module OK: {doctype}")
            frappe.clear_cache(doctype=doctype)

    if custom_field_has_module():
        for custom_field in [
            "Stock Reconciliation-custom_inventory_campaign_section",
            "Stock Reconciliation-custom_inventory_source",
            "Stock Reconciliation-custom_inventory_campaign",
            "Stock Reconciliation-custom_inventory_session_refs",
        ]:
            if frappe.db.exists("Custom Field", custom_field):
                normalize_custom_field_module(custom_field)

    frappe.clear_cache(doctype="Stock Reconciliation")



def normalize_custom_field_module(custom_field_name):
    if not custom_field_has_module():
        return

    current_module = frappe.db.get_value("Custom Field", custom_field_name, "module")
    if current_module != MODULE:
        frappe.db.set_value("Custom Field", custom_field_name, "module", MODULE, update_modified=False)
        print(f"Normalized Custom Field module: {custom_field_name} -> {MODULE}")



def mark_legacy_doctypes_if_present():
    """
    Do not delete previous development structures automatically.

    Inventory Access Token and Inventory Agent Assignment may exist on a site that
    already ran previous scripts. Sprint 1 now uses token fields on Inventory Agent
    and explicit authorized_locations / authorized_item_groups. We keep legacy DocTypes
    dormant to avoid destructive migrations during design stabilization.
    """

    for doctype_name in sorted(LEGACY_DOCTYPES):
        if frappe.db.exists("DocType", doctype_name):
            print(f"Legacy DocType present but not used by Sprint 1 model: {doctype_name}")
