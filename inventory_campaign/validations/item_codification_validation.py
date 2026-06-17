"""
Validation code for Item codification.

Place this file in:
    apps/inventory_campaign/inventory_campaign/validations/item_codification_validation.py

Then add/merge the hook shown in hooks_snippet.py into hooks.py.
"""

from __future__ import annotations

import frappe
from frappe import _


def validate_item_codification(doc, method=None):
    """Validate Item codification fields and child table.

    Rules:
    - Non-coded items may remain empty.
    - If codification data exists, Famille and Sous Famille must be coherent.
    - Maximum 5 characteristics.
    - Never more than 1 major characteristic.
    - Exactly 1 major characteristic when characteristics are provided.
    - Property cannot be duplicated in the same Item.
    - Unit is controlled by Propriete.unite, not typed freely on Item.
    """

    famille = doc.get("custom_famille")
    sous_famille = doc.get("custom_sous_famille")
    rows = list(doc.get("custom_caracteristiques") or [])

    validate_family_and_subfamily(famille, sous_famille, rows)
    validate_characteristics(rows)


def validate_family_and_subfamily(famille, sous_famille, rows):
    if famille and not sous_famille:
        frappe.throw(_("Sous Famille est obligatoire si Famille est renseignée."))

    if sous_famille and not famille:
        frappe.throw(_("Famille est obligatoire si Sous Famille est renseignée."))

    if famille and sous_famille:
        expected_famille = frappe.db.get_value("Sous Famille", sous_famille, "famille")
        if not expected_famille:
            frappe.throw(_("La Sous Famille {0} n'existe pas ou n'a pas de Famille.").format(sous_famille))

        if expected_famille != famille:
            frappe.throw(
                _("La Sous Famille {0} appartient à la Famille {1}, pas à la Famille {2}.").format(
                    sous_famille, expected_famille, famille
                )
            )

    if rows and (not famille or not sous_famille):
        frappe.throw(_("Famille et Sous Famille sont obligatoires si des caractéristiques sont renseignées."))


def validate_characteristics(rows):
    if len(rows) > 5:
        frappe.throw(_("Un article ne peut pas avoir plus de 5 caractéristiques."))

    major_rows = []
    seen_properties = set()
    seen_sequences = set()

    for row in rows:
        row_label = _("ligne {0}").format(row.idx or "?")

        if not row.sequence:
            row.sequence = row.idx

        if row.sequence < 1 or row.sequence > 5:
            frappe.throw(_("Le Rang doit être compris entre 1 et 5 sur la {0}.").format(row_label))

        if row.sequence in seen_sequences:
            frappe.throw(_("Le Rang {0} est utilisé plusieurs fois dans les caractéristiques.").format(row.sequence))
        seen_sequences.add(row.sequence)

        if not row.propriete:
            frappe.throw(_("La Propriété est obligatoire sur la {0} des caractéristiques.").format(row_label))

        if row.propriete in seen_properties:
            frappe.throw(_("La Propriété {0} est répétée plusieurs fois sur cet article.").format(row.propriete))
        seen_properties.add(row.propriete)

        propriete = frappe.db.get_value(
            "Propriete",
            row.propriete,
            ["unite", "value_type", "valeurs_possibles"],
            as_dict=True,
        )
        if not propriete:
            frappe.throw(_("La Propriété {0} n'existe pas.").format(row.propriete))

        # Unit is owned by Propriete. It is copied here for visibility/import convenience.
        row.unite = propriete.unite or None

        if row.is_major:
            major_rows.append(row)

        validate_value_against_property(row, propriete)

    if len(major_rows) > 1:
        frappe.throw(_("Un article ne peut jamais avoir plus d'une caractéristique majeure."))

    if rows and len(major_rows) != 1:
        frappe.throw(_("Un article codifié doit avoir exactement une caractéristique majeure."))


def validate_value_against_property(row, propriete):
    """Validate value only when a value is already entered.

    Values may remain empty during initial parametrage.
    Later, inventory sessions may propose corrections exceptionally.
    """
    value = row.valeur
    if isinstance(value, str):
        value = value.strip()

    if value in (None, ""):
        return

    value_type = propriete.value_type or "Text"

    if value_type == "Number":
        try:
            int(str(value))
        except Exception:
            frappe.throw(_("La valeur '{0}' doit être un nombre entier pour la propriété {1}.").format(value, row.propriete))

    elif value_type == "Decimal":
        try:
            float(str(value).replace(",", "."))
        except Exception:
            frappe.throw(_("La valeur '{0}' doit être un nombre décimal pour la propriété {1}.").format(value, row.propriete))

    elif value_type == "Select":
        allowed_values = get_allowed_values(propriete.valeurs_possibles)
        if allowed_values and str(value).strip() not in allowed_values:
            frappe.throw(
                _("La valeur '{0}' n'est pas autorisée pour la propriété {1}. Valeurs autorisées : {2}").format(
                    value, row.propriete, ", ".join(allowed_values)
                )
            )


def get_allowed_values(raw_values):
    if not raw_values:
        return []

    # Accept both line-separated and comma-separated values for convenience.
    normalized = raw_values.replace(",", "\n")
    return [line.strip() for line in normalized.splitlines() if line.strip()]
