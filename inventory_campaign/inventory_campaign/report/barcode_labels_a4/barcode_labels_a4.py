# Copyright (c) 2026, Inventory Campaign contributors
# For license information, please see license.txt

from __future__ import annotations

import base64
import re
from collections import defaultdict
from functools import lru_cache

import frappe
from frappe import _


CODE39_PATTERNS = {
	"0": "bsbSBsBsb",
	"1": "BsbSbsbsB",
	"2": "bsBSbsbsB",
	"3": "BsBSbsbsb",
	"4": "bsbSBsbsB",
	"5": "BsbSBsbsb",
	"6": "bsBSBsbsb",
	"7": "bsbSbsBsB",
	"8": "BsbSbsBsb",
	"9": "bsBSbsBsb",
	"A": "BsbsbSbsB",
	"B": "bsBsbSbsB",
	"C": "BsBsbSbsb",
	"D": "bsbsBSbsB",
	"E": "BsbsBSbsb",
	"F": "bsBsBSbsb",
	"G": "bsbsbSBsB",
	"H": "BsbsbSBsb",
	"I": "bsBsbSBsb",
	"J": "bsbsBSBsb",
	"K": "BsbsbsbSB",
	"L": "bsBsbsbSB",
	"M": "BsBsbsbSb",
	"N": "bsbsBsbSB",
	"O": "BsbsBsbSb",
	"P": "bsBsBsbSb",
	"Q": "bsbsbsBSB",
	"R": "BsbsbsBSb",
	"S": "bsBsbsBSb",
	"T": "bsbsBsBSb",
	"U": "BSbsbsbsB",
	"V": "bSBsbsbsB",
	"W": "BSBsbsbsb",
	"X": "bSbsBsbsB",
	"Y": "BSbsBsbsb",
	"Z": "bSBsBsbsb",
	"-": "bSbsbsBsB",
	".": "BSbsbsBsb",
	" ": "bSBsbsBsb",
	"*": "bSbsBsBsb",
	"$": "bSbSbSbsb",
	"/": "bSbSbsbSb",
	"+": "bSbsbSbSb",
	"%": "bsbSbSbSb",
}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = get_columns()

	pasted_codes = parse_pasted_codes(filters.get("codes_to_print"))
	rows = get_barcode_rows(filters, pasted_codes)
	missing_codes = []

	if pasted_codes:
		rows, missing_codes = match_pasted_codes(rows, pasted_codes)

	for sequence, row in enumerate(rows, start=1):
		row["sequence"] = sequence
		row["barcode_svg"] = make_code39_data_uri(row.get("barcode"))
		row["barcode_render_error"] = 0 if row["barcode_svg"] else 1

	message = build_message(rows, pasted_codes, missing_codes)
	return columns, rows, message


def get_columns():
	return [
		{
			"fieldname": "sequence",
			"label": _("No."),
			"fieldtype": "Int",
			"width": 60,
		},
		{
			"fieldname": "barcode",
			"label": _("Barcode"),
			"fieldtype": "Data",
			"width": 150,
		},
		{
			"fieldname": "item_code",
			"label": _("Item Code"),
			"fieldtype": "Link",
			"options": "Item",
			"width": 150,
		},
		{
			"fieldname": "item_name",
			"label": _("Item Name"),
			"fieldtype": "Data",
			"width": 280,
		},
		{
			"fieldname": "item_group",
			"label": _("Item Group"),
			"fieldtype": "Link",
			"options": "Item Group",
			"width": 160,
		},
		{
			"fieldname": "uom",
			"label": _("UOM"),
			"fieldtype": "Link",
			"options": "UOM",
			"width": 90,
		},
		{
			"fieldname": "barcode_type",
			"label": _("Barcode Type"),
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"fieldname": "barcode_svg",
			"label": _("Barcode Image"),
			"fieldtype": "Data",
			"hidden": 1,
		},
		{
			"fieldname": "barcode_render_error",
			"label": _("Render Error"),
			"fieldtype": "Check",
			"hidden": 1,
		},
	]


def get_barcode_rows(filters, pasted_codes):
	"""Return Item records and use Item.item_code as the barcode value.

	``tabItem`` is the authoritative source. The printed barcode, pasted-code
	matching, From/To range and alphanumeric ordering all use ``item_code``.
	"""
	conditions = ["IFNULL(i.item_code, '') != ''"]
	params = []

	if int(filters.get("enabled_only", 1) or 0):
		conditions.append("i.disabled = 0")

	if int(filters.get("stock_items_only", 1) or 0):
		conditions.append("i.is_stock_item = 1")

	item_group = (filters.get("item_group") or "").strip()
	if item_group:
		group_bounds = frappe.db.get_value(
			"Item Group", item_group, ["lft", "rgt"], as_dict=True
		)
		if not group_bounds:
			frappe.throw(_("Unknown Item Group: {0}").format(item_group))

		conditions.append("ig.lft >= %s AND ig.rgt <= %s")
		params.extend([group_bounds.lft, group_bounds.rgt])

	if pasted_codes:
		unique_codes = sorted(set(pasted_codes))
		placeholders = ", ".join(["%s"] * len(unique_codes))
		conditions.append(f"i.item_code IN ({placeholders})")
		params.extend(unique_codes)
	else:
		from_code = (filters.get("from_code") or "").strip()
		to_code = (filters.get("to_code") or "").strip()

		if from_code:
			conditions.append("CAST(i.item_code AS BINARY) >= CAST(%s AS BINARY)")
			params.append(from_code)

		if to_code:
			conditions.append("CAST(i.item_code AS BINARY) <= CAST(%s AS BINARY)")
			params.append(to_code)

	query = f"""
		SELECT
			i.item_code AS barcode,
			'CODE-39' AS barcode_type,
			i.stock_uom AS uom,
			i.name AS item_code,
			i.item_name AS item_name,
			i.item_group AS item_group
		FROM `tabItem` i
		INNER JOIN `tabItem Group` ig
			ON ig.name = i.item_group
		WHERE {" AND ".join(conditions)}
		ORDER BY CAST(i.item_code AS BINARY)
	"""

	return frappe.db.sql(query, tuple(params), as_dict=True)


def parse_pasted_codes(raw_value):
	raw_value = raw_value or ""
	return [code for code in re.split(r"[\s,;]+", raw_value.strip()) if code]


def match_pasted_codes(rows, requested_codes):
	by_barcode = defaultdict(list)
	by_item = defaultdict(list)

	for row in rows:
		by_barcode[str(row.get("barcode") or "")].append(row)
		by_item[str(row.get("item_code") or "")].append(row)

	matched = []
	missing = []

	# Keep duplicate pasted entries, but sort alphanumerically as requested.
	for requested_code in sorted(requested_codes):
		candidates = by_barcode.get(requested_code) or by_item.get(requested_code)
		if candidates:
			matched.append(frappe._dict(candidates[0].copy()))
		else:
			missing.append(requested_code)

	return matched, missing


def build_message(rows, pasted_codes, missing_codes):
	page_count = (len(rows) + 23) // 24
	parts = [
		_("{0} barcode label(s), {1} A4 page(s), 24 labels per page.").format(
			len(rows), page_count
		)
	]

	if pasted_codes:
		parts.append(_("Pasted-code selection has priority over the From/To filters."))

	if missing_codes:
		preview = ", ".join(missing_codes[:30])
		if len(missing_codes) > 30:
			preview += _(" … and {0} more").format(len(missing_codes) - 30)
		parts.append(_("Codes not found: {0}").format(preview))

	return "<br>".join(parts)


@lru_cache(maxsize=8192)
def make_code39_data_uri(value):
	value = str(value or "").upper()
	if not value or any(character not in CODE39_PATTERNS or character == "*" for character in value):
		return ""

	x_position = 0
	path_commands = []
	encoded_value = f"*{value}*"

	for character_index, character in enumerate(encoded_value):
		pattern = CODE39_PATTERNS[character]

		for element in pattern:
			width = 3 if element.isupper() else 1
			if element.lower() == "b":
				path_commands.append(
					f"M{x_position} 0h{width}v100h-{width}z"
				)
			x_position += width

		if character_index < len(encoded_value) - 1:
			x_position += 1

	svg = (
		f'<svg xmlns="http://www.w3.org/2000/svg" '
		f'viewBox="0 0 {x_position} 100" preserveAspectRatio="none">'
		f'<path fill="#000" d="{"".join(path_commands)}"/>'
		f"</svg>"
	)
	encoded_svg = base64.b64encode(svg.encode("utf-8")).decode("ascii")
	return f"data:image/svg+xml;base64,{encoded_svg}"
