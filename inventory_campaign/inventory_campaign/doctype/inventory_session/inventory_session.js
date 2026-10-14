// Copyright (c) 2026, Richard Amouzou and contributors
// Inventory audit actions: shares one server-side cleaning/roll-forward engine.

frappe.ui.form.on("Inventory Session", {
    refresh(frm) {
        if (frm.is_new() || frm.doc.docstatus !== 1) return;

        // First action in the Inventory group: audit-approved zero-variance stock.
        frm.add_custom_button(
            __("Validate Zero-Variance Stock & Locations"),
            () => validate_zero_variance_stock(frm),
            __("Inventory")
        );

        frm.add_custom_button(
            __("Create Inventory Count Session"),
            () => create_inventory_count_session(frm),
            __("Inventory")
        );

        show_inventory_session_summary(frm);
    },
});

function inventory_audit_call(frm) {
    return frappe.call({
        method: "inventory_campaign.inventory_campaign.doctype.inventory_session.inventory_session.get_inventory_session_audit",
        args: { inventory_session: frm.doc.name },
    });
}

function audit_number(value, digits = 2) {
    return Number(value || 0).toLocaleString(undefined, {
        minimumFractionDigits: 0,
        maximumFractionDigits: digits,
    });
}

function audit_escape(value) {
    return String(value === null || value === undefined ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function show_inventory_session_summary(frm) {
    // Render inside the HTML field `summary` in the third header column.
    // No document field value is changed or saved.
    const field = frm.fields_dict.summary;
    if (!field || !field.$wrapper || !field.$wrapper.length) return;
    const root = field.$wrapper;
    root.find(".inventory-audit-session-summary").remove();
    const panel = $('<div class="inventory-audit-session-summary" style="margin:16px 0;padding:15px;border:1px solid var(--border-color);border-radius:8px;background:var(--card-bg)"></div>');
    panel.html('<div style="font-weight:600;margin-bottom:8px">' + __("Session Summary") +
        '</div><div class="text-muted">' + __("Calculating inventory reconciliation...") + '</div>');
    root.prepend(panel);

    inventory_audit_call(frm).then((r) => {
        const data = r.message || {};
        const s = data.summary || {};
        const metrics = [
            [__("Raw Lines"), s.raw_lines],
            [__("Duplicates Removed"), s.duplicates_removed],
            [__("Matched"), s.matched],
            [__("Moved Matches"), s.moved],
            [__("Ambiguous Matches"), s.ambiguous],
            [__("Zero-Variance Items"), s.zero_variance_items],
            [__("Zero-Variance Location Changes"), s.relocated_zero_variance_items],
            [__("Items with Variance"), s.variance_items],
            [__("Recount Rows"), s.recount_rows],
        ];
        const grid = metrics.map(([label, value]) =>
            `<div style="padding:8px 10px;border:1px solid var(--border-color);border-radius:6px">` +
            `<div class="text-muted" style="font-size:12px">${audit_escape(label)}</div>` +
            `<strong>${audit_number(value, 0)}</strong></div>`
        ).join("");
        panel.html(`<div style="font-weight:600;margin-bottom:10px">${audit_escape(__("Session Summary"))}</div>` +
            `<div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px">${grid}</div>` +
            `<div style="margin-top:12px"><button type="button" class="btn btn-default btn-sm inventory-audit-view-report">` +
            audit_escape(__("View Zero-Variance Audit Report")) + `</button></div>`);
        panel.find(".inventory-audit-view-report").on("click", () => show_zero_variance_report(data.report || [], frm.doc.name));
    }).catch(() => {
        panel.find(".text-muted").text(__("Unable to calculate the audit summary. Retry by refreshing the form."));
    });
}

function show_zero_variance_report(rows, session_name) {
    const columns = [
        ["item_code", "Item Code"], ["item_name", "Item Name"],
        ["previous_warehouses", "Previous Warehouse(s)"],
        ["warehouse", "Current / Counted Warehouse"],
        ["quality_status", "Quality"],
        ["historical_system_qty", "Historical System Qty"],
        ["historical_physical_qty", "Historical Physical Qty"],
        ["current_system_qty", "Current System Qty"],
        ["adjusted_physical_qty", "Adjusted Physical Qty"],
        ["valuation_rate_ct", "Rate / CT"], ["stock_value", "Stock Value"],
        ["is_relocation", "Movement"],
    ];
    const numeric = new Set([
        "historical_system_qty", "historical_physical_qty", "current_system_qty",
        "adjusted_physical_qty", "valuation_rate_ct", "stock_value"
    ]);
    const value_for = (row, key, formatted) => {
        let value = row[key];
        if (key === "previous_warehouses") value = Array.isArray(value) ? value.join(", ") : (value || "");
        if (key === "is_relocation") value = value ? __("Location change") : __("Conform");
        if (numeric.has(key)) {
            const number = Number(value);
            return formatted ? audit_number(Number.isFinite(number) ? number : 0, 4)
                : (Number.isFinite(number) ? String(number) : "0");
        }
        return value === null || value === undefined ? "" : String(value);
    };
    const report_title = __("Zero-Variance Inventory Audit Report");
    const session_label = session_name || "";
    const heading = `<h3>${audit_escape(report_title)}</h3>` +
        `<p>${audit_escape(__("Inventory Session"))}: ${audit_escape(session_label)} &nbsp; | &nbsp;` +
        `${audit_escape(__("Rows"))}: ${rows.length}</p>`;
    const table = `<table class="table table-bordered table-condensed">` +
        `<thead><tr>${columns.map(c => `<th>${audit_escape(__(c[1]))}</th>`).join("")}</tr></thead>` +
        `<tbody>${rows.map(row => `<tr>${columns.map(([key]) =>
            `<td>${audit_escape(value_for(row, key, true))}</td>`
        ).join("")}</tr>`).join("")}</tbody></table>`;
    const content = rows.length ? heading + table : heading + `<p>${audit_escape(__("No zero-variance Items found."))}</p>`;
    const dialog = new frappe.ui.Dialog({
        title: report_title,
        size: "extra-large",
        fields: [{ fieldtype: "HTML", fieldname: "report_html" }],
    });
    dialog.fields_dict.report_html.$wrapper.html(
        `<div style="overflow-x:auto;max-height:60vh">${content}</div>`
    );

    const export_csv = () => {
        // Raw numeric values (not localized) keep CSV usable for calculations in Excel.
        const csv_cell = value => {
            const text = String(value === null || value === undefined ? "" : value);
            return '"' + text.replace(/"/g, '""') + '"';
        };
        const lines = [
            columns.map(([, label]) => csv_cell(__(label))).join(";"),
            ...rows.map(row => columns.map(([key]) => csv_cell(value_for(row, key, false))).join(";"))
        ];
        const blob = new Blob(["\uFEFF" + lines.join("\r\n")], {
            type: "text/csv;charset=utf-8"
        });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `zero_variance_audit_${String(session_label).replace(/[^a-zA-Z0-9_-]/g, "_") || "session"}.csv`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    };

    const print_report = () => {
        const printable = window.open("", "_blank");
        if (!printable) {
            frappe.msgprint(__("Please allow pop-ups to print the audit report."));
            return;
        }
        printable.document.open();
        printable.document.write(
            '<!DOCTYPE html><html><head><meta charset="utf-8"><title>' +
            audit_escape(report_title) +
            '</title><style>' +
            '@page{size:A4 landscape;margin:12mm}' +
            'body{font-family:Arial,sans-serif;color:#222;font-size:10px}' +
            'h3{font-size:18px;margin-bottom:7px}p{margin-bottom:15px}' +
            'table{width:100%;border-collapse:collapse}' +
            'th,td{border:1px solid #888;padding:5px;word-break:break-word}' +
            'th{background:#eee}thead{display:table-header-group}' +
            'tr{break-inside:avoid}' +
            '</style></head><body>' + content + '</body></html>'
        );
        printable.document.close();
        printable.focus();
        // Allow the print window to finish rendering before opening print dialog.
        setTimeout(() => {
            if (!printable.closed) printable.print();
        }, 450);
    };

    const footer = dialog.$wrapper.find(".modal-footer");
    const print_button = $('<button type="button" class="btn btn-default btn-sm"></button>')
        .text(__("Print / PDF"))
        .on("click", print_report);
    const export_button = $('<button type="button" class="btn btn-primary btn-sm"></button>')
        .text(__("Export CSV (Excel)"))
        .on("click", export_csv);
    footer.prepend(export_button).prepend(print_button);
    dialog.show();
}

function transfer_inventory_session(frm, mode) {
    frappe.call({
        method: "inventory_campaign.inventory_campaign.doctype.inventory_session.inventory_session.create_inventory_count_session",
        args: { inventory_session: frm.doc.name, mode },
        freeze: true,
        freeze_message: __("Processing inventory reconciliation..."),
        callback(r) {
            if (r.exc || !r.message) return;
            const result = r.message;
            if (!result.created) {
                frappe.msgprint({title: __("Inventory Audit"), message: __(result.message), indicator: "green"});
                show_inventory_session_summary(frm);
                return;
            }
            frappe.show_alert({
                message: __("Inventory Count Session {0} created in Draft.", [result.name]),
                indicator: "green",
            });
            frappe.set_route("Form", "Inventory Count Session", result.name);
        },
    });
}

function validate_zero_variance_stock(frm) {
    inventory_audit_call(frm).then(r => {
        if (r.exc || !r.message) return;
        const info = r.message;
        show_zero_variance_report(info.report || [], frm.doc.name);
        if (!Number((info.summary || {}).relocated_zero_variance_items || 0)) {
            frappe.msgprint({title: __("Inventory Audit"),
                message: __("Zero-variance stock accepted: no location changes require an Inventory Count Session."),
                indicator: "green"});
            return;
        }
        frappe.confirm(__("The zero-variance audit report is ready. Create a Draft Inventory Count Session ONLY for location changes?"),
            () => transfer_inventory_session(frm, "zero_relocation"));
    });
}

function create_inventory_count_session(frm) {
    frappe.confirm(__("Create a Draft Inventory Count Session only for Items with a non-zero global variance?"),
        () => transfer_inventory_session(frm, "variance"));
}
