// Copyright (c) 2026, Richard Amouzou and contributors
// For license information, please see license.txt

const barcode_labels_a4_report = {
	filters: [
		{
			fieldname: "item_group",
			label: __("Item Group"),
			fieldtype: "Link",
			options: "Item Group",
			description: __("Optional. Includes the selected group and its descendants."),
		},
		{
			fieldname: "from_code",
			label: __("From Code"),
			fieldtype: "Data",
			description: __("Optional alphanumeric lower bound."),
		},
		{
			fieldname: "to_code",
			label: __("To Code"),
			fieldtype: "Data",
			description: __("Optional alphanumeric upper bound."),
		},
		{
			fieldname: "codes_to_print",
			label: __("Codes to Print"),
			fieldtype: "Small Text",
			description: __("Paste codes separated by lines, spaces, commas, semicolons, or tabs. This list takes priority over From/To Code."),
		},
		{
			fieldname: "enabled_only",
			label: __("Enabled Items Only"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "stock_items_only",
			label: __("Stock Items Only"),
			fieldtype: "Check",
			default: 1,
		},
	],

	onload(report) {
		const validate_rows = () => {
			if (!report.data || !report.data.length) {
				frappe.msgprint(__("No barcode label is available for the selected filters."));
				return false;
			}
			return true;
		};

		report.page.add_inner_button(__("Preview A4 Labels"), () => {
			if (!validate_rows()) return;
			open_barcode_labels_a4(report.data, false);
		});

		report.page.add_inner_button(
			__("Print / Save PDF"),
			() => {
				if (!validate_rows()) return;
				open_barcode_labels_a4(report.data, true);
			},
			__("Actions")
		);
	},
};

function barcode_labels_escape_html(value) {
	return String(value ?? "")
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;")
		.replace(/'/g, "&#039;");
}

function barcode_labels_binary_sort(rows) {
	return [...rows].sort((left, right) => {
		const a = String(left.barcode ?? "");
		const b = String(right.barcode ?? "");
		return a < b ? -1 : a > b ? 1 : 0;
	});
}

function open_barcode_labels_a4(source_rows, auto_print) {
	const rows = barcode_labels_binary_sort(source_rows).filter(
		(row) => row && row.barcode && row.barcode_svg
	);

	if (!rows.length) {
		frappe.msgprint(__("No printable CODE-39 barcode was found."));
		return;
	}

	const labels_per_page = 24;
	const rows_per_column = 12;
	const page_count = Math.ceil(rows.length / labels_per_page);
	let pages_html = "";

	for (let page_number = 0; page_number < page_count; page_number += 1) {
		let labels_html = "";

		for (let slot = 0; slot < labels_per_page; slot += 1) {
			const data_index = page_number * labels_per_page + slot;
			if (data_index >= rows.length) break;

			const row_number = slot < rows_per_column ? slot : slot - rows_per_column;
			const column_number = slot < rows_per_column ? 0 : 1;
			const left_position = column_number === 0 ? 0 : 99.75;
			const top_position = row_number * 23.8333;
			const barcode = barcode_labels_escape_html(rows[data_index].barcode);
			const barcode_svg = barcode_labels_escape_html(rows[data_index].barcode_svg);

			labels_html += `
				<div class="barcode-label" style="left:${left_position}mm;top:${top_position}mm;">
					<img class="barcode-image" src="${barcode_svg}" alt="${barcode}">
					<div class="barcode-number">${barcode}</div>
				</div>`;
		}

		pages_html += `<section class="barcode-page">${labels_html}</section>`;
	}

	const print_window = window.open("", "_blank");
	if (!print_window) {
		frappe.msgprint(__("The browser blocked the print preview window. Allow pop-ups for this ERPNext site."));
		return;
	}

	print_window.document.open();
	print_window.document.write(`<!doctype html>
<html>
<head>
	<meta charset="utf-8">
	<title>Barcode Labels A4</title>
	<style>
		@page { size: A4 portrait; margin: 6mm 7mm; }
		* { box-sizing: border-box; }
		html, body { margin: 0; padding: 0; background: #fff; }
		body { font-family: Arial, Helvetica, sans-serif; }
		.preview-toolbar {
			position: sticky; top: 0; z-index: 10; display: flex; gap: 8px;
			align-items: center; padding: 8px 12px; background: #fff;
			border-bottom: 1px solid #ddd; font-size: 13px;
		}
		.preview-toolbar button {
			border: 1px solid #bbb; border-radius: 5px; padding: 6px 12px;
			background: #f7f7f7; cursor: pointer;
		}
		.preview-toolbar .summary { margin-left: auto; color: #555; }
		.barcode-page {
			position: relative; width: 196mm; height: 285mm;
			margin: 0 auto; padding: 0; overflow: hidden;
			page-break-after: always; break-after: page; background: #fff;
		}
		.barcode-page:last-child { page-break-after: auto; break-after: auto; }
		.barcode-label {
			position: absolute; width: 96.25mm; height: 22.8333mm;
			border: 0.55pt dashed #999; border-radius: 2mm;
			overflow: hidden; background: #fff;
			page-break-inside: avoid; break-inside: avoid;
		}
		.barcode-image {
			position: absolute; left: 4mm; top: 2.3mm;
			width: 88.25mm; height: 8.6mm; display: block;
		}
		.barcode-number {
			position: absolute; left: 0; top: 11.25mm; width: 100%; height: 5mm;
			font-size: 12.4pt; font-weight: 700; line-height: 5mm;
			text-align: center; white-space: nowrap; color: #000;
		}
		@media screen {
			body { background: #e9ecef; }
			.barcode-page { margin: 12px auto; box-shadow: 0 1px 8px rgba(0,0,0,.18); }
		}
		@media print {
			.preview-toolbar { display: none !important; }
			body { background: #fff; }
			.barcode-page { margin: 0; box-shadow: none; }
		}
	</style>
</head>
<body>
	<div class="preview-toolbar">
		<button type="button" onclick="window.print()">Print / Save PDF</button>
		<button type="button" onclick="window.close()">Close</button>
		<span class="summary">${rows.length} label(s) - ${page_count} A4 page(s) - 2 columns x 12 rows</span>
	</div>
	${pages_html}
	<script>
		window.addEventListener('load', function () {
			if (${auto_print ? "true" : "false"}) {
				setTimeout(function () { window.print(); }, 350);
			}
		});
	<\/script>
</body>
</html>`);
	print_window.document.close();
}

// Register both aliases because some installations route by scrubbed name.
frappe.query_reports["Barcode Labels A4"] = barcode_labels_a4_report;
frappe.query_reports["barcode_labels_a4"] = barcode_labels_a4_report;
