"""
One-time setup script for Inventory Campaign codification.

Purpose
-------
1) Create STANDARD DocType JSON files inside the Inventory Campaign app for:
   - Famille
   - Sous Famille
   - Propriete
   - Caracteristique Article (child table)

2) Reload those DocTypes so they are created as standard app DocTypes, not custom DocTypes.

3) Add Custom Fields to ERPNext Item under a dedicated Codification tab:
   - Famille
   - Sous Famille
   - Caracteristiques child table
   - Nouveau Code
   - Nouvelle Description

Important
---------
This is NOT a patch. Run it once with bench execute.
Because it writes DocType JSON files into the app folder, it must be executed in an environment
where the bench process has write permission on apps/inventory_campaign/inventory_campaign.

Place this file in:
    apps/inventory_campaign/inventory_campaign/scripts/create_codification_standard_doctypes.py

Run:
    bench --site <site-name> execute inventory_campaign.scripts.create_codification_standard_doctypes.execute

Example:
    bench --site erpv15dev.marsavco.com execute inventory_campaign.scripts.create_codification_standard_doctypes.execute
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import frappe
from frappe import _
from frappe.utils import now


APP_NAME = "inventory_campaign"
MODULE_NAME = "Inventory Campaign"
MODULE_FOLDER = frappe.scrub(MODULE_NAME)  # inventory_campaign


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------

def execute() -> dict[str, Any]:
    """Entry point for bench execute."""
    frappe.only_for("System Manager")

    ensure_module_def()
    ensure_module_package_dir()

    written_files = []
    reloaded_doctypes = []

    for doctype_doc in get_standard_doctype_documents():
        paths = write_standard_doctype_files(doctype_doc)
        written_files.extend(str(path) for path in paths)

        reload_standard_doctype(doctype_doc["name"])
        force_standard_doctype(doctype_doc["name"])
        reloaded_doctypes.append(doctype_doc["name"])

    created_or_updated_fields = create_item_custom_fields()

    frappe.clear_cache()
    frappe.clear_cache(doctype="Item")
    frappe.db.commit()

    return {
        "status": "success",
        "app": APP_NAME,
        "module": MODULE_NAME,
        "standard_doctypes_reloaded": reloaded_doctypes,
        "doctype_files_written": written_files,
        "item_custom_fields_created_or_updated": created_or_updated_fields,
        "next_step": "Add/merge the Item validate hook from hooks_snippet.py and run bench clear-cache + bench restart.",
    }


# -----------------------------------------------------------------------------
# App / module helpers
# -----------------------------------------------------------------------------

def get_app_path() -> Path:
    """Return apps/inventory_campaign/inventory_campaign."""
    return Path(frappe.get_app_path(APP_NAME))


def get_module_path() -> Path:
    """Return apps/inventory_campaign/inventory_campaign/inventory_campaign."""
    return get_app_path() / MODULE_FOLDER


def ensure_module_def() -> None:
    """Ensure the Module Def exists and points to inventory_campaign."""
    if frappe.db.exists("Module Def", MODULE_NAME):
        module_def = frappe.get_doc("Module Def", MODULE_NAME)
        changed = False
        if hasattr(module_def, "app_name") and module_def.app_name != APP_NAME:
            module_def.app_name = APP_NAME
            changed = True
        if changed:
            module_def.save(ignore_permissions=True)
        return

    module_def = frappe.new_doc("Module Def")
    module_def.module_name = MODULE_NAME
    if hasattr(module_def, "app_name"):
        module_def.app_name = APP_NAME
    module_def.insert(ignore_permissions=True)


def ensure_module_package_dir() -> None:
    module_dir = get_module_path()
    doctype_dir = module_dir / "doctype"
    module_dir.mkdir(parents=True, exist_ok=True)
    doctype_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "__init__.py").touch(exist_ok=True)
    (doctype_dir / "__init__.py").touch(exist_ok=True)


def folder_name(doctype_name: str) -> str:
    return frappe.scrub(doctype_name)


def class_name(doctype_name: str) -> str:
    return "".join(part.capitalize() for part in frappe.scrub(doctype_name).split("_"))


# -----------------------------------------------------------------------------
# Standard DocType file generation
# -----------------------------------------------------------------------------

def write_standard_doctype_files(doctype_doc: dict[str, Any]) -> list[Path]:
    """Write the standard DocType JSON + minimal Python controller into the app's module folder."""
    doctype_name = doctype_doc["name"]
    folder = get_module_path() / "doctype" / folder_name(doctype_name)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "__init__.py").touch(exist_ok=True)

    json_path = folder / f"{folder_name(doctype_name)}.json"
    json_path.write_text(
        json.dumps(doctype_doc, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    py_path = folder / f"{folder_name(doctype_name)}.py"
    py_path.write_text(
        'from frappe.model.document import Document\n\n\n'
        f'class {class_name(doctype_name)}(Document):\n'
        '    pass\n',
        encoding="utf-8",
    )

    return [json_path, py_path]


def reload_standard_doctype(doctype_name: str) -> None:
    """Reload a standard DocType from the JSON file just written."""
    frappe.reload_doc(MODULE_FOLDER, "doctype", folder_name(doctype_name), force=True)


def force_standard_doctype(doctype_name: str) -> None:
    """If a previous attempt created it as custom, force the metadata flag back to standard."""
    if frappe.db.exists("DocType", doctype_name):
        frappe.db.set_value("DocType", doctype_name, "custom", 0, update_modified=False)
        frappe.db.set_value("DocType", doctype_name, "module", MODULE_NAME, update_modified=False)


def base_doctype_doc(
    *,
    name: str,
    fields: list[dict[str, Any]],
    istable: int = 0,
    autoname: str | None = None,
    title_field: str | None = None,
    search_fields: str | None = None,
    permissions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a standard DocType JSON document."""
    doc = {
        "actions": [],
        "allow_import": 1 if not istable else 0,
        "allow_rename": 1 if not istable else 0,
        "autoname": autoname or "",
        "creation": now(),
        "custom": 0,
        "doctype": "DocType",
        "document_type": "Setup" if not istable else "",
        "editable_grid": 1 if istable else 0,
        "engine": "InnoDB",
        "field_order": [field["fieldname"] for field in fields],
        "fields": fields,
        "grid_page_length": 50,
        "hide_toolbar": 0,
        "idx": 0,
        "image_field": "",
        "in_create": 0,
        "index_web_pages_for_search": 1,
        "is_submittable": 0,
        "issingle": 0,
        "istable": istable,
        "links": [],
        "modified": now(),
        "modified_by": "Administrator",
        "module": MODULE_NAME,
        "name": name,
        "naming_rule": "By fieldname" if autoname and autoname.startswith("field:") else "",
        "owner": "Administrator",
        "permissions": permissions or ([] if istable else system_manager_permissions()),
        "sort_field": "modified",
        "sort_order": "DESC",
        "states": [],
        "title_field": title_field or "",
        "track_changes": 1 if not istable else 0,
        "quick_entry": 0,
    }

    if search_fields:
        doc["search_fields"] = search_fields

    return doc


def system_manager_permissions() -> list[dict[str, Any]]:
    return [
        {
            "role": "System Manager",
            "read": 1,
            "write": 1,
            "create": 1,
            "delete": 1,
            "submit": 0,
            "cancel": 0,
            "amend": 0,
            "print": 1,
            "email": 1,
            "report": 1,
            "export": 1,
            "share": 1,
        }
    ]


def get_standard_doctype_documents() -> list[dict[str, Any]]:
    return [
        get_famille_doctype(),
        get_sous_famille_doctype(),
        get_propriete_doctype(),
        get_caracteristique_article_doctype(),
    ]


def get_famille_doctype() -> dict[str, Any]:
    fields = [
        {
            "label": "Code",
            "fieldname": "code",
            "fieldtype": "Data",
            "reqd": 1,
            "unique": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
            "bold": 1,
        },
        {
            "label": "Description",
            "fieldname": "description",
            "fieldtype": "Data",
            "reqd": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
        },
    ]
    return base_doctype_doc(
        name="Famille",
        fields=fields,
        autoname="field:code",
        title_field="description",
        search_fields="code,description",
    )


def get_sous_famille_doctype() -> dict[str, Any]:
    fields = [
        {
            "label": "Code",
            "fieldname": "code",
            "fieldtype": "Data",
            "reqd": 1,
            "unique": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
            "bold": 1,
        },
        {
            "label": "Description",
            "fieldname": "description",
            "fieldtype": "Data",
            "reqd": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
        },
        {
            "label": "Famille",
            "fieldname": "famille",
            "fieldtype": "Link",
            "options": "Famille",
            "reqd": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
        },
    ]
    return base_doctype_doc(
        name="Sous Famille",
        fields=fields,
        autoname="field:code",
        title_field="description",
        search_fields="code,description,famille",
    )


def get_propriete_doctype() -> dict[str, Any]:
    fields = [
        {
            "label": "Code",
            "fieldname": "code",
            "fieldtype": "Data",
            "reqd": 1,
            "unique": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
            "bold": 1,
        },
        {
            "label": "Description",
            "fieldname": "description",
            "fieldtype": "Data",
            "reqd": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
        },
        {
            "label": "Unité",
            "fieldname": "unite",
            "fieldtype": "Link",
            "options": "UOM",
            "in_list_view": 1,
        },
        {
            "label": "Type de valeur",
            "fieldname": "value_type",
            "fieldtype": "Select",
            "options": "Text\nNumber\nDecimal\nSelect",
            "default": "Text",
            "reqd": 1,
            "in_list_view": 1,
        },
        {
            "label": "Valeurs possibles",
            "fieldname": "valeurs_possibles",
            "fieldtype": "Small Text",
            "description": "Une valeur par ligne. Utilisé seulement si Type de valeur = Select.",
        },
    ]
    return base_doctype_doc(
        name="Propriete",
        fields=fields,
        autoname="field:code",
        title_field="description",
        search_fields="code,description,unite,value_type",
    )


def get_caracteristique_article_doctype() -> dict[str, Any]:
    fields = [
        {
            "label": "Rang",
            "fieldname": "sequence",
            "fieldtype": "Int",
            "reqd": 1,
            "in_list_view": 1,
            "columns": 1,
        },
        {
            "label": "Propriété",
            "fieldname": "propriete",
            "fieldtype": "Link",
            "options": "Propriete",
            "reqd": 1,
            "in_list_view": 1,
            "columns": 2,
        },
        {
            "label": "Valeur",
            "fieldname": "valeur",
            "fieldtype": "Data",
            "in_list_view": 1,
            "columns": 2,
        },
        {
            "label": "Unité",
            "fieldname": "unite",
            "fieldtype": "Link",
            "options": "UOM",
            "read_only": 1,
            "in_list_view": 1,
            "columns": 1,
            "description": "Récupérée automatiquement depuis la propriété.",
        },
        {
            "label": "Caractéristique majeure",
            "fieldname": "is_major",
            "fieldtype": "Check",
            "in_list_view": 1,
            "columns": 1,
        },
    ]
    return base_doctype_doc(
        name="Caracteristique Article",
        fields=fields,
        istable=1,
    )


# -----------------------------------------------------------------------------
# Custom Fields on ERPNext Item
# -----------------------------------------------------------------------------

def create_item_custom_fields() -> list[str]:
    fields = [
        {
            "label": "Codification",
            "fieldname": "custom_codification_tab",
            "fieldtype": "Tab Break",
            "insert_after": "description",
        },
        {
            "label": "Classification",
            "fieldname": "custom_codification_classification_section",
            "fieldtype": "Section Break",
            "insert_after": "custom_codification_tab",
        },
        {
            "label": "Famille",
            "fieldname": "custom_famille",
            "fieldtype": "Link",
            "options": "Famille",
            "insert_after": "custom_codification_classification_section",
        },
        {
            "label": "",
            "fieldname": "custom_codification_classification_column",
            "fieldtype": "Column Break",
            "insert_after": "custom_famille",
        },
        {
            "label": "Sous Famille",
            "fieldname": "custom_sous_famille",
            "fieldtype": "Link",
            "options": "Sous Famille",
            "insert_after": "custom_codification_classification_column",
        },
        {
            "label": "Caractéristiques",
            "fieldname": "custom_codification_caracteristiques_section",
            "fieldtype": "Section Break",
            "insert_after": "custom_sous_famille",
        },
        {
            "label": "Caractéristiques",
            "fieldname": "custom_caracteristiques",
            "fieldtype": "Table",
            "options": "Caracteristique Article",
            "insert_after": "custom_codification_caracteristiques_section",
        },
        {
            "label": "Résultat de codification",
            "fieldname": "custom_codification_resultat_section",
            "fieldtype": "Section Break",
            "insert_after": "custom_caracteristiques",
        },
        {
            "label": "Nouveau Code",
            "fieldname": "custom_nouveau_code",
            "fieldtype": "Data",
            "insert_after": "custom_codification_resultat_section",
            "description": "Code proposé ou futur code normalisé.",
        },
        {
            "label": "Nouvelle Description",
            "fieldname": "custom_nouvelle_description",
            "fieldtype": "Small Text",
            "insert_after": "custom_nouveau_code",
            "description": "Description standardisée proposée.",
        },
    ]

    updated = []
    for field in fields:
        upsert_custom_field("Item", field)
        updated.append(f"Item-{field['fieldname']}")

    return updated


def upsert_custom_field(dt: str, field: dict[str, Any]) -> None:
    """Create or update a Custom Field on a standard ERPNext DocType."""
    fieldname = field["fieldname"]
    name = f"{dt}-{fieldname}"

    payload = {"dt": dt, **field}

    # Set Custom Field module to Inventory Campaign when the Frappe version supports it.
    try:
        custom_field_meta = frappe.get_meta("Custom Field")
        if custom_field_meta.has_field("module"):
            payload["module"] = MODULE_NAME
    except Exception:
        pass

    if frappe.db.exists("Custom Field", name):
        custom_field = frappe.get_doc("Custom Field", name)
        for key, value in payload.items():
            if custom_field.meta.has_field(key) or key in {
                "dt",
                "fieldname",
                "fieldtype",
                "label",
                "options",
                "insert_after",
            }:
                setattr(custom_field, key, value)
        custom_field.save(ignore_permissions=True)
    else:
        payload["doctype"] = "Custom Field"
        frappe.get_doc(payload).insert(ignore_permissions=True)
