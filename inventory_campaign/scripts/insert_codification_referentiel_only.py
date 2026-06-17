"""
Insert Inventory Campaign codification reference data only.

This script is intentionally limited to master/setup DocTypes:
- Famille
- Sous Famille
- Propriete
- Sous Famille Propriete, only if this DocType already exists

It DOES NOT update Item and DOES NOT create codification on items.
The Item update is handled later by update_item_codification_after_completion.py.

Important:
- Insertions are done with frappe.get_doc({...}) + doc.insert(...)
- No Data Import
- No direct SQL insert
- No bulk insert
- Sous Famille.name is forced as Famille-Code to avoid collisions:
    EE-CA, ME-CA, CO-CA, etc.
- Sous Famille.code must NOT be unique, because short codes repeat across families.
- Statut values like "Validé terrain" are normalized to:
    statut = "Validé"
    source = "Terrain"

Place this file in:
    apps/inventory_campaign/inventory_campaign/scripts/insert_codification_referentiel_only.py

Place the CSV files in:
    apps/inventory_campaign/inventory_campaign/seeds/codification/

Dry run:
    bench --site erpv15dev.marsavco.com execute inventory_campaign.scripts.insert_codification_referentiel_only.execute --kwargs '{"dry_run": 1}'

Real insert:
    bench --site erpv15dev.marsavco.com execute inventory_campaign.scripts.insert_codification_referentiel_only.execute
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
    dry_run: int = 0,
    update_existing: int = 1,
    insert_rules_if_doctype_exists: int = 1,
    fail_on_missing_required_reference: int = 1,
    seed_dir: str | None = None,
) -> dict[str, Any]:
    """Insert only the codification master data."""

    frappe.only_for("System Manager")

    dry_run = int(dry_run or 0)
    update_existing = int(update_existing or 0)
    insert_rules_if_doctype_exists = int(insert_rules_if_doctype_exists or 0)
    fail_on_missing_required_reference = int(fail_on_missing_required_reference or 0)

    if seed_dir:
        seed_path = Path(seed_dir)
    else:
        seed_path = Path(frappe.get_app_path(APP_NAME)) / SEED_RELATIVE_PATH

    if not seed_path.exists():
        frappe.throw(_("Seed folder not found: {0}").format(seed_path))

    ensure_required_doctypes()

    result: dict[str, Any] = {
        "status": "dry_run" if dry_run else "success",
        "seed_dir": str(seed_path),
        "dry_run": dry_run,
        "update_existing": update_existing,
        "families_inserted_or_seen": 0,
        "sub_families_inserted_or_seen": 0,
        "properties_inserted_or_seen": 0,
        "rules_inserted_or_seen": 0,
        "rules_skipped_doctype_missing": 0,
        "rows_skipped": 0,
        "warnings": [],
        "errors": [],
    }

    validate_uom_resolution(seed_path, result)

    try:
        seed_familles(
            seed_path / "famille.csv",
            result,
            dry_run=bool(dry_run),
            update_existing=bool(update_existing),
        )

        seed_sous_familles(
            seed_path / "sous_famille.csv",
            result,
            dry_run=bool(dry_run),
            update_existing=bool(update_existing),
            strict=bool(fail_on_missing_required_reference),
        )

        seed_proprietes(
            seed_path / "propriete.csv",
            result,
            dry_run=bool(dry_run),
            update_existing=bool(update_existing),
            strict=bool(fail_on_missing_required_reference),
        )

        if insert_rules_if_doctype_exists:
            if frappe.db.exists("DocType", "Sous Famille Propriete"):
                seed_sous_famille_propriete_rules(
                    seed_path / "sous_famille_propriete_rules.csv",
                    result,
                    dry_run=bool(dry_run),
                    update_existing=bool(update_existing),
                    strict=bool(fail_on_missing_required_reference),
                )
            else:
                result["rules_skipped_doctype_missing"] = count_csv_rows(
                    seed_path / "sous_famille_propriete_rules.csv"
                )
                result["warnings"].append(
                    "DocType 'Sous Famille Propriete' not found. "
                    "Rule rows were not inserted. Famille, Sous Famille and Propriete were still processed."
                )

        if result["errors"] and fail_on_missing_required_reference:
            frappe.throw("\n".join(result["errors"][:80]))

        if dry_run:
            frappe.db.rollback()
        else:
            frappe.db.commit()
            frappe.clear_cache()

        return result

    except Exception:
        frappe.db.rollback()
        raise


# -----------------------------------------------------------------------------
# CSV helpers
# -----------------------------------------------------------------------------

def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        frappe.throw(_("CSV file not found: {0}").format(path))

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = []
        for row in csv.DictReader(f):
            rows.append(clean_row(row))
        return rows


def clean_row(row: dict[str, Any]) -> dict[str, str]:
    cleaned: dict[str, str] = {}

    for key, value in row.items():
        if key is None:
            continue

        cleaned[clean(key)] = clean(value)

    return cleaned


def count_csv_rows(path: Path) -> int:
    return len(read_csv(path)) if path.exists() else 0


def clean(value: Any) -> str:
    if value is None:
        return ""

    return str(value).replace("\u00a0", " ").strip()


def get_value(row: dict[str, Any], *keys: str, default: str = "") -> str:
    """Return first non-empty value among possible column names."""

    for key in keys:
        key = clean(key)

        if key in row and clean(row.get(key)):
            return clean(row.get(key))

        lower_key = key.lower()

        for existing_key, value in row.items():
            if clean(existing_key).lower() == lower_key and clean(value):
                return clean(value)

    return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(clean(value)))
    except Exception:
        return default


def to_check(value: Any) -> int:
    return 1 if clean(value).lower() in {"1", "yes", "oui", "true", "y"} else 0


def make_sous_famille_name(famille: str, code_or_name: str) -> str:
    """Force Sous Famille technical name as Famille-Code.

    The short code is not globally unique:
    CA can mean several different sub-families depending on Famille.
    """

    famille = clean(famille)
    code_or_name = clean(code_or_name)

    if not code_or_name:
        return ""

    if famille and code_or_name.startswith(f"{famille}-"):
        return code_or_name

    if famille and "-" not in code_or_name:
        return f"{famille}-{code_or_name}"

    return code_or_name


def normalize_statut(value: Any) -> str | None:
    """Normalize terrain statuses to the Select values allowed by Frappe."""

    raw = clean(value)

    if not raw:
        return None

    normalized = (
        raw.lower()
        .replace("é", "e")
        .replace("è", "e")
        .replace("ê", "e")
        .replace("à", "a")
        .strip()
    )

    mapping = {
        "valide": "Validé",
        "valide terrain": "Validé",
        "validé": "Validé",
        "validé terrain": "Validé",
        "non valide": "Non validé",
        "non validé": "Non validé",
        "a revoir": "A revoir",
        "à revoir": "A revoir",
        "draft": "Draft",
    }

    return mapping.get(normalized, None)


def infer_source_from_statut(statut_raw: Any, source_raw: Any) -> str | None:
    """If Statut contains 'terrain', move that information to Source."""

    source = clean(source_raw)

    if source:
        return source

    statut = clean(statut_raw).lower()

    if "terrain" in statut:
        return "Terrain"

    return None


# -----------------------------------------------------------------------------
# Meta validation
# -----------------------------------------------------------------------------

def ensure_required_doctypes() -> None:
    required = {
        "Famille": ["code", "description"],
        "Sous Famille": ["code", "description", "famille"],
        "Propriete": ["code", "description", "unite", "value_type", "valeurs_possibles"],
    }

    missing_messages = []

    for doctype, fields in required.items():
        if not frappe.db.exists("DocType", doctype):
            missing_messages.append(f"DocType missing: {doctype}")
            continue

        meta = frappe.get_meta(doctype)

        for fieldname in fields:
            if not meta.has_field(fieldname):
                missing_messages.append(f"Field missing: {doctype}.{fieldname}")

    if missing_messages:
        frappe.throw(
            "Codification DocTypes are not ready. Run the one-time DocType creation script first.\n"
            + "\n".join(missing_messages)
        )


def validate_uom_resolution(seed_dir: Path, result: dict[str, Any]) -> None:
    """Report UOM candidates that were deliberately not created.

    This script never creates UOMs. If the Propriete CSV has a resolved UOM,
    it must already exist.
    """

    candidate_path = seed_dir / "uom_a_creer_candidates.csv"

    if candidate_path.exists() and count_csv_rows(candidate_path):
        result["warnings"].append(
            "UOM candidates exist in uom_a_creer_candidates.csv. "
            "They were NOT created automatically. Properties with no safe existing UOM keep unite blank."
        )


# -----------------------------------------------------------------------------
# Insert / update helper
# -----------------------------------------------------------------------------

def upsert_doc(
    doctype: str,
    name: str,
    values: dict[str, Any],
    *,
    dry_run: bool = False,
    update_existing: bool = True,
) -> str:
    """Return inserted / updated / unchanged / skipped_existing / dry_run.

    Insertions use:
        doc = frappe.get_doc({...})
        doc.insert(ignore_permissions=True)
    """

    name = clean(name)

    if not name:
        frappe.throw(f"Cannot insert {doctype}: empty name")

    values = {k: v for k, v in values.items() if k != "name"}

    if frappe.db.exists(doctype, name):
        if dry_run:
            return "dry_run_existing"

        if not update_existing:
            return "skipped_existing"

        doc = frappe.get_doc(doctype, name)

        changed = False

        for fieldname, value in values.items():
            if getattr(doc, fieldname, None) != value:
                setattr(doc, fieldname, value)
                changed = True

        if changed:
            doc.save(ignore_permissions=True)
            return "updated"

        return "unchanged"

    if dry_run:
        return "dry_run_insert"

    payload = {
        "doctype": doctype,
        "name": name,
    }
    payload.update(values)

    doc = frappe.get_doc(payload)

    # Force the provided name instead of allowing autoname to create CA, DI, VE...
    doc.flags.name_set = True

    doc.insert(ignore_permissions=True)

    return "inserted"


def add_error_or_warning(result: dict[str, Any], message: str, *, strict: bool) -> None:
    if strict:
        result["errors"].append(message)
    else:
        result["warnings"].append(message)
        result["rows_skipped"] += 1


# -----------------------------------------------------------------------------
# Seed functions
# -----------------------------------------------------------------------------

def seed_familles(
    path: Path,
    result: dict[str, Any],
    *,
    dry_run: bool,
    update_existing: bool,
) -> None:
    for row in read_csv(path):
        code = get_value(row, "code", "Code", "name", "Name")
        description = get_value(row, "description", "Description")

        if not code:
            result["rows_skipped"] += 1
            continue

        upsert_doc(
            "Famille",
            code,
            {
                "code": code,
                "description": description,
            },
            dry_run=dry_run,
            update_existing=update_existing,
        )

        result["families_inserted_or_seen"] += 1


def seed_sous_familles(
    path: Path,
    result: dict[str, Any],
    *,
    dry_run: bool,
    update_existing: bool,
    strict: bool,
) -> None:
    for row in read_csv(path):
        code = get_value(row, "code", "Code")
        description = get_value(row, "description", "Description")
        famille = get_value(row, "famille", "Famille", "code_famille", "Code Famille")

        if not code or not famille:
            result["rows_skipped"] += 1
            continue

        # Important:
        # Ignore row.get("name") deliberately.
        # Short codes like CA, DI, VE are not globally unique.
        name = make_sous_famille_name(famille, code)

        if not dry_run and not frappe.db.exists("Famille", famille):
            add_error_or_warning(
                result,
                f"Sous Famille {name}: Famille missing: {famille}",
                strict=strict,
            )
            continue

        upsert_doc(
            "Sous Famille",
            name,
            {
                "code": code,
                "description": description,
                "famille": famille,
            },
            dry_run=dry_run,
            update_existing=update_existing,
        )

        result["sub_families_inserted_or_seen"] += 1


def seed_proprietes(
    path: Path,
    result: dict[str, Any],
    *,
    dry_run: bool,
    update_existing: bool,
    strict: bool,
) -> None:
    for row in read_csv(path):
        code = get_value(row, "code", "Code", "name", "Name")
        description = get_value(row, "description", "Description")
        unite = get_value(row, "unite", "Unite", "uom", "UOM")
        value_type = get_value(row, "value_type", "Type de valeur", default="Text")
        valeurs_possibles = get_value(row, "valeurs_possibles", "Valeurs possibles")

        if not code:
            result["rows_skipped"] += 1
            continue

        if unite and not dry_run and not frappe.db.exists("UOM", unite):
            add_error_or_warning(
                result,
                f"Propriete {code}: UOM missing: {unite}",
                strict=strict,
            )

            if strict:
                continue

            unite = ""

        if valeurs_possibles:
            valeurs_possibles = valeurs_possibles.replace("\\n", "\n")

        upsert_doc(
            "Propriete",
            code,
            {
                "code": code,
                "description": description,
                "unite": unite or None,
                "value_type": value_type or "Text",
                "valeurs_possibles": valeurs_possibles or None,
            },
            dry_run=dry_run,
            update_existing=update_existing,
        )

        result["properties_inserted_or_seen"] += 1


def seed_sous_famille_propriete_rules(
    path: Path,
    result: dict[str, Any],
    *,
    dry_run: bool,
    update_existing: bool,
    strict: bool,
) -> None:
    """Insert rules if the target DocType exists.

    Expected DocType fieldnames:
    - sous_famille: Link Sous Famille
    - propriete: Link Propriete

    Optional fieldnames supported:
    - famille
    - sequence
    - is_major_default
    - is_required
    - raw_unit
    - resolved_uom
    - valeurs_possibles
    - statut
    - source
    - masque_description_erp
    """

    doctype = "Sous Famille Propriete"
    meta = frappe.get_meta(doctype)

    for row in read_csv(path):
        famille = get_value(row, "famille", "Famille")
        sous_famille_raw = get_value(row, "sous_famille", "Sous Famille")
        propriete = get_value(row, "propriete", "Propriete", "propriété", "Propriété")
        sequence = to_int(get_value(row, "sequence", "Sequence", "rang", "Rang"), 0)

        if not sous_famille_raw or not propriete:
            result["rows_skipped"] += 1
            continue

        sous_famille = make_sous_famille_name(famille, sous_famille_raw)

        if not dry_run:
            if famille and not frappe.db.exists("Famille", famille):
                add_error_or_warning(
                    result,
                    f"Rule {sous_famille}/{propriete}: Famille missing: {famille}",
                    strict=strict,
                )
                continue

            if not frappe.db.exists("Sous Famille", sous_famille):
                add_error_or_warning(
                    result,
                    f"Rule {sous_famille}/{propriete}: Sous Famille missing",
                    strict=strict,
                )
                continue

            if not frappe.db.exists("Propriete", propriete):
                add_error_or_warning(
                    result,
                    f"Rule {sous_famille}/{propriete}: Propriete missing",
                    strict=strict,
                )
                continue

        raw_unit = get_value(row, "raw_unit", "Unite source", "Unité source")
        resolved_uom = get_value(row, "resolved_uom", "UOM resolue", "UOM résolue")
        valeurs_possibles = get_value(row, "valeurs_possibles", "Valeurs possibles")
        raw_statut = get_value(row, "statut", "Statut")
        raw_source = get_value(row, "source", "Source")

        if resolved_uom and not dry_run and not frappe.db.exists("UOM", resolved_uom):
            add_error_or_warning(
                result,
                f"Rule {sous_famille}/{propriete}: UOM missing: {resolved_uom}",
                strict=strict,
            )

            if strict:
                continue

            resolved_uom = ""

        if valeurs_possibles:
            valeurs_possibles = valeurs_possibles.replace("\\n", "\n")

        name = f"{sous_famille}-{sequence:02d}-{propriete}"

        candidates: dict[str, Any] = {
            "sous_famille": sous_famille,
            "famille": famille or None,
            "propriete": propriete,
            "sequence": sequence,
            "is_major_default": to_check(
                get_value(row, "is_major_default", "Majeure par defaut", "is_major")
            ),
            "is_required": to_check(
                get_value(row, "is_required", "Obligatoire")
            ),
            "raw_unit": raw_unit or None,
            "resolved_uom": resolved_uom or None,
            "valeurs_possibles": valeurs_possibles or None,
            "statut": normalize_statut(raw_statut),
            "source": infer_source_from_statut(raw_statut, raw_source),
            "masque_description_erp": get_value(
                row,
                "masque_description_erp",
                "Masque Description ERP",
                "Masque de description ERP",
            ) or None,
        }

        values: dict[str, Any] = {}

        for fieldname, value in candidates.items():
            if meta.has_field(fieldname):
                values[fieldname] = value

        upsert_doc(
            doctype,
            name,
            values,
            dry_run=dry_run,
            update_existing=update_existing,
        )

        result["rules_inserted_or_seen"] += 1