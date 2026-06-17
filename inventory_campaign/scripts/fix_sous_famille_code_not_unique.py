from __future__ import annotations

import frappe


DOCTYPE = "Sous Famille"
TABLE = "tabSous Famille"


def execute():
    frappe.only_for("System Manager")

    if not frappe.db.exists("DocType", DOCTYPE):
        frappe.throw(f"DocType not found: {DOCTYPE}")

    result = {
        "doctype": DOCTYPE,
        "docfield_unique_changed": 0,
        "custom_field_unique_changed": 0,
        "property_setters_deleted": 0,
        "indexes_dropped": [],
    }

    # 1. Corriger le DocField standard
    doc = frappe.get_doc("DocType", DOCTYPE)

    changed = False
    for df in doc.fields:
        if df.fieldname == "code":
            if getattr(df, "unique", 0):
                df.unique = 0
                changed = True
                result["docfield_unique_changed"] += 1

    if changed:
        doc.save(ignore_permissions=True)

    # 2. Sécurité : corriger aussi tabDocField directement
    frappe.db.set_value(
        "DocField",
        {"parent": DOCTYPE, "fieldname": "code"},
        "unique",
        0,
        update_modified=False,
    )

    # 3. Sécurité : s'il existe un Custom Field parasite
    custom_fields = frappe.get_all(
        "Custom Field",
        filters={"dt": DOCTYPE, "fieldname": "code"},
        pluck="name",
    )

    for cf_name in custom_fields:
        cf = frappe.get_doc("Custom Field", cf_name)
        if getattr(cf, "unique", 0):
            cf.unique = 0
            cf.save(ignore_permissions=True)
            result["custom_field_unique_changed"] += 1

    # 4. Supprimer les Property Setter qui remettraient unique = 1
    property_setters = frappe.get_all(
        "Property Setter",
        filters={
            "doc_type": DOCTYPE,
            "field_name": "code",
            "property": "unique",
        },
        pluck="name",
    )

    for ps in property_setters:
        frappe.delete_doc("Property Setter", ps, ignore_permissions=True)
        result["property_setters_deleted"] += 1

    frappe.clear_cache(doctype=DOCTYPE)

    # 5. Recharger le schema Frappe
    frappe.db.updatedb(DOCTYPE)

    # 6. Supprimer les index UNIQUE encore présents en base sur la colonne code
    indexes = frappe.db.sql(
        f"""
        SHOW INDEX FROM `{TABLE}`
        WHERE Column_name = 'code'
          AND Non_unique = 0
          AND Key_name != 'PRIMARY'
        """,
        as_dict=True,
    )

    seen = set()

    for idx in indexes:
        key_name = idx.get("Key_name")

        if not key_name or key_name in seen:
            continue

        seen.add(key_name)
        frappe.db.sql(f"ALTER TABLE `{TABLE}` DROP INDEX `{key_name}`")
        result["indexes_dropped"].append(key_name)

    frappe.clear_cache(doctype=DOCTYPE)
    frappe.db.commit()

    return result
