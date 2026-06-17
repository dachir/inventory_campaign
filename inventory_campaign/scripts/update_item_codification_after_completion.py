"""
Update ERPNext Item codification after the reference data and the article mapping are complete.

This script is deliberately separated from the reference-data insertion script.
Run it only when the article mapping workbook is complete and validated.

It updates only ERPNext Item custom codification fields:
- custom_famille
- custom_sous_famille
- custom_caracteristiques child table
- custom_nouveau_code
- custom_nouvelle_description

It DOES NOT rename Item and DOES NOT change item_code.

Place this file in:
    apps/inventory_campaign/inventory_campaign/scripts/update_item_codification_after_completion.py

Place CSV files in:
    apps/inventory_campaign/inventory_campaign/seeds/codification/

Dry run:
    bench --site erpv15dev.marsavco.com execute inventory_campaign.scripts.update_item_codification_after_completion.execute --kwargs '{"dry_run": 1}'

Actual update, when ready:
    bench --site erpv15dev.marsavco.com execute inventory_campaign.scripts.update_item_codification_after_completion.execute --kwargs '{"confirm": "YES", "dry_run": 0}'
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import frappe
from frappe import _

APP_NAME = "inventory_campaign"
SEED_RELATIVE_PATH = Path("seeds") / "codification"


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------

def execute(
    confirm: str = "NO",
    dry_run: int = 1,
    clear_existing_characteristics: int = 1,
    item_update_csv: str = "item_update_mapped.csv",
    char_csv: str = "item_caracteristiques.csv",
    fail_on_error: int = 1,
    commit_every: int = 200,
) -> dict[str, Any]:
    """Update Item codification fields after final mapping validation.

    By default this runs as dry_run. Actual update requires confirm="YES".
    """
    frappe.only_for("System Manager")

    if not int(dry_run) and confirm != "YES":
        frappe.throw('Actual Item update requires --kwargs \'{"confirm": "YES", "dry_run": 0}\'')

    seed_dir = Path(frappe.get_app_path(APP_NAME)) / SEED_RELATIVE_PATH
    if not seed_dir.exists():
        frappe.throw(_("Seed folder not found: {0}").format(seed_dir))

    ensure_item_custom_fields()

    result = {
        "status": "dry_run" if int(dry_run) else "success",
        "seed_dir": str(seed_dir),
        "item_update_csv": item_update_csv,
        "char_csv": char_csv,
        "dry_run": int(dry_run),
        "items_seen": 0,
        "items_updated": 0,
        "items_skipped_unmapped": 0,
        "items_missing": 0,
        "characteristic_rows_seen": 0,
        "characteristic_rows_inserted": 0,
        "errors": [],
        "warnings": [],
    }

    item_rows = read_csv(seed_dir / item_update_csv)
    char_by_item = load_characteristics_by_item(seed_dir / char_csv, result)

    processed_since_commit = 0

    for row in item_rows:
        item_code = clean(row.get("item_code"))
        if not item_code:
            continue
        result["items_seen"] += 1

        famille = clean(row.get("famille"))
        sous_famille = clean(row.get("sous_famille"))

        if not famille or not sous_famille:
            result["items_skipped_unmapped"] += 1
            continue

        if not frappe.db.exists("Item", item_code):
            result["items_missing"] += 1
            add_issue(result, f"Item missing: {item_code}", fail_on_error=bool(fail_on_error))
            continue

        item_chars = char_by_item.get(item_code, [])
        if not validate_item_row_references(item_code, famille, sous_famille, item_chars, result, fail_on_error=bool(fail_on_error)):
            continue

        if int(dry_run):
            result["items_updated"] += 1
            result["characteristic_rows_inserted"] += len(item_chars)
            continue

        item = frappe.get_doc("Item", item_code)
        item.custom_famille = famille
        item.custom_sous_famille = sous_famille
        item.custom_nouveau_code = clean(row.get("nouveau_code")) or None
        item.custom_nouvelle_description = clean(row.get("nouvelle_description")) or None

        if int(clear_existing_characteristics):
            item.set("custom_caracteristiques", [])

        for char in sorted(item_chars, key=lambda x: to_int(x.get("sequence"))):
            item.append(
                "custom_caracteristiques",
                {
                    "sequence": to_int(char.get("sequence")),
                    "propriete": clean(char.get("propriete")),
                    "valeur": clean(char.get("valeur")) or None,
                    "unite": clean(char.get("unite")) or None,
                    "is_major": to_check(char.get("is_major")),
                },
            )
            result["characteristic_rows_inserted"] += 1

        item.save(ignore_permissions=True)
        result["items_updated"] += 1
        processed_since_commit += 1

        if processed_since_commit >= int(commit_every):
            frappe.db.commit()
            processed_since_commit = 0

    if result["errors"] and int(fail_on_error):
        frappe.db.rollback()
        frappe.throw("\n".join(result["errors"][:80]))

    if not int(dry_run):
        frappe.db.commit()
        frappe.clear_cache(doctype="Item")

    return result


# -----------------------------------------------------------------------------
# CSV helpers
# -----------------------------------------------------------------------------

def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        frappe.throw(_("CSV file not found: {0}").format(path))
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\u00a0", " ").strip()


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(clean(value)))
    except Exception:
        return default


def to_check(value: Any) -> int:
    return 1 if clean(value).lower() in {"1", "yes", "oui", "true", "y"} else 0


def add_issue(result: dict[str, Any], message: str, *, fail_on_error: bool) -> None:
    if fail_on_error:
        result["errors"].append(message)
    else:
        result["warnings"].append(message)


# -----------------------------------------------------------------------------
# Validation helpers
# -----------------------------------------------------------------------------

def ensure_item_custom_fields() -> None:
    meta = frappe.get_meta("Item")
    required_fields = [
        "custom_famille",
        "custom_sous_famille",
        "custom_caracteristiques",
        "custom_nouveau_code",
        "custom_nouvelle_description",
    ]
    missing = [field for field in required_fields if not meta.has_field(field)]
    if missing:
        frappe.throw(
            "Item codification custom fields are not ready. Run the one-time DocType/custom-field setup first.\n"
            + "Missing fields: "
            + ", ".join(missing)
        )


def load_characteristics_by_item(path: Path, result: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    char_by_item: dict[str, list[dict[str, str]]] = {}
    for row in read_csv(path):
        item_code = clean(row.get("item_code"))
        if not item_code:
            continue
        char_by_item.setdefault(item_code, []).append(row)
        result["characteristic_rows_seen"] += 1
    return char_by_item


def validate_item_row_references(
    item_code: str,
    famille: str,
    sous_famille: str,
    chars: list[dict[str, str]],
    result: dict[str, Any],
    *,
    fail_on_error: bool,
) -> bool:
    ok = True

    if not frappe.db.exists("Famille", famille):
        add_issue(result, f"{item_code}: Famille missing: {famille}", fail_on_error=fail_on_error)
        ok = False

    if not frappe.db.exists("Sous Famille", sous_famille):
        add_issue(result, f"{item_code}: Sous Famille missing: {sous_famille}", fail_on_error=fail_on_error)
        ok = False
    else:
        sf_famille = frappe.db.get_value("Sous Famille", sous_famille, "famille")
        if sf_famille and sf_famille != famille:
            add_issue(
                result,
                f"{item_code}: Sous Famille {sous_famille} belongs to {sf_famille}, not {famille}",
                fail_on_error=fail_on_error,
            )
            ok = False

    if len(chars) > 5:
        add_issue(result, f"{item_code}: more than 5 characteristics ({len(chars)})", fail_on_error=fail_on_error)
        ok = False

    major_count = sum(1 for c in chars if to_check(c.get("is_major")))
    if major_count != 1:
        add_issue(result, f"{item_code}: expected exactly one major characteristic, found {major_count}", fail_on_error=fail_on_error)
        ok = False

    seen_props: set[str] = set()
    for char in chars:
        prop = clean(char.get("propriete"))
        if not prop:
            add_issue(result, f"{item_code}: empty Propriete in characteristics", fail_on_error=fail_on_error)
            ok = False
            continue
        if prop in seen_props:
            add_issue(result, f"{item_code}: duplicate Propriete {prop}", fail_on_error=fail_on_error)
            ok = False
        seen_props.add(prop)

        if not frappe.db.exists("Propriete", prop):
            add_issue(result, f"{item_code}: Propriete missing: {prop}", fail_on_error=fail_on_error)
            ok = False
            continue

        expected_uom = clean(frappe.db.get_value("Propriete", prop, "unite"))
        row_uom = clean(char.get("unite"))
        if row_uom and not frappe.db.exists("UOM", row_uom):
            add_issue(result, f"{item_code}: UOM missing in characteristic {prop}: {row_uom}", fail_on_error=fail_on_error)
            ok = False
        if expected_uom and row_uom and expected_uom != row_uom:
            add_issue(
                result,
                f"{item_code}: UOM mismatch for {prop}: CSV={row_uom}, Propriete={expected_uom}",
                fail_on_error=fail_on_error,
            )
            ok = False

    return ok
