from __future__ import annotations

import frappe

DOCTYPE = "Sous Famille Propriete"
MODULE = "Inventory Campaign"


FIELDS = [
    {
        "label": "Sous Famille",
        "fieldname": "sous_famille",
        "fieldtype": "Link",
        "options": "Sous Famille",
        "reqd": 1,
        "in_list_view": 1,
    },
    {
        "label": "Famille",
        "fieldname": "famille",
        "fieldtype": "Link",
        "options": "Famille",
        "in_list_view": 1,
    },
    {
        "label": "Propriete",
        "fieldname": "propriete",
        "fieldtype": "Link",
        "options": "Propriete",
        "reqd": 1,
        "in_list_view": 1,
    },
    {
        "label": "Sequence",
        "fieldname": "sequence",
        "fieldtype": "Int",
        "reqd": 1,
        "in_list_view": 1,
    },
    {
        "label": "Majeure par defaut",
        "fieldname": "is_major_default",
        "fieldtype": "Check",
        "in_list_view": 1,
    },
    {
        "label": "Obligatoire",
        "fieldname": "is_required",
        "fieldtype": "Check",
    },
    {
        "label": "Unite source",
        "fieldname": "raw_unit",
        "fieldtype": "Data",
    },
    {
        "label": "UOM resolue",
        "fieldname": "resolved_uom",
        "fieldtype": "Link",
        "options": "UOM",
    },
    {
        "label": "Valeurs possibles",
        "fieldname": "valeurs_possibles",
        "fieldtype": "Small Text",
    },
    {
        "label": "Statut",
        "fieldname": "statut",
        "fieldtype": "Select",
        "options": "\nValidé\nNon validé\nA revoir\nDraft",
    },
    {
        "label": "Source",
        "fieldname": "source",
        "fieldtype": "Data",
    },
    {
        "label": "Masque Description ERP",
        "fieldname": "masque_description_erp",
        "fieldtype": "Small Text",
    },
]


def execute():
    frappe.only_for("System Manager")

    ensure_module_def()
    created = ensure_doctype()

    frappe.clear_cache()
    frappe.db.commit()

    return {
        "status": "success",
        "doctype": DOCTYPE,
        "created": created,
        "message": "DocType Sous Famille Propriete prêt pour insertion des règles.",
    }


def ensure_module_def():
    if frappe.db.exists("Module Def", MODULE):
        return

    module_def = frappe.new_doc("Module Def")
    module_def.module_name = MODULE

    if hasattr(module_def, "app_name"):
        module_def.app_name = "inventory_campaign"

    module_def.insert(ignore_permissions=True)


def ensure_doctype() -> bool:
    if frappe.db.exists("DocType", DOCTYPE):
        doc = frappe.get_doc("DocType", DOCTYPE)
        changed = False

        if doc.module != MODULE:
            doc.module = MODULE
            changed = True

        existing_fields = {df.fieldname for df in doc.fields}

        for field in FIELDS:
            if field["fieldname"] not in existing_fields:
                doc.append("fields", field)
                changed = True

        if not doc.permissions:
            add_permissions(doc)
            changed = True

        if changed:
            doc.save(ignore_permissions=True)

        return False

    doc = frappe.get_doc(
        {
            "doctype": "DocType",
            "name": DOCTYPE,
            "module": MODULE,
            "custom": 0,
            "is_submittable": 0,
            "track_changes": 1,
            "allow_rename": 1,
            "fields": FIELDS,
            "permissions": [],
        }
    )

    add_permissions(doc)
    doc.insert(ignore_permissions=True)

    return True


def add_permissions(doc):
    doc.append(
        "permissions",
        {
            "role": "System Manager",
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
        },
    )
