frappe.ui.form.on("Inventory Count Session", {
    refresh(frm) {
        if (frm.doc.docstatus !== 0) {
            return;
        }

        frm.add_custom_button(__("Initialize Inventory"), () => {
            initialize_inventory(frm);
        });
    },
});

frappe.ui.form.on("Inventory Count Session Detail", {
    physical_count(frm, cdt, cdn) {
        recalculate_inventory_row(frm, cdt, cdn);
    },

    qty_in_ct(frm, cdt, cdn) {
        recalculate_inventory_row(frm, cdt, cdn);
    },

    stock_valuation(frm, cdt, cdn) {
        recalculate_inventory_row(frm, cdt, cdn);
    },

    table_uawl_remove(frm) {
        recalculate_header_difference_amount(frm);
    },
});

function initialize_inventory(frm) {
    const warehouses = get_selected_warehouses(frm);

    if (!frm.doc.inventory_campaign) {
        frappe.msgprint({
            title: __("Missing Inventory Campaign"),
            message: __("Select an Inventory Campaign before initializing the inventory."),
            indicator: "red",
        });
        return;
    }

    if (!frm.doc.inventory_date) {
        frappe.msgprint({
            title: __("Missing Inventory Date"),
            message: __("Select the Inventory Date before initializing the inventory."),
            indicator: "red",
        });
        return;
    }

    if (!frm.doc.branch) {
        frappe.msgprint({
            title: __("Missing Branch"),
            message: __("Select the Branch before initializing the inventory."),
            indicator: "red",
        });
        return;
    }

    if (!warehouses.length) {
        frappe.msgprint({
            title: __("Missing Warehouses"),
            message: __("Select at least one Warehouse in Inventory Count Warehouses."),
            indicator: "red",
        });
        return;
    }

    const detail_field = get_inventory_detail_field(frm);
    const existing_rows = (frm.doc[detail_field] || []).length;

    const warning = existing_rows
        ? __(
            "This session already contains {0} inventory line(s). Continuing will permanently clear these lines and all Physical Count values before loading the new snapshot for Branch {1} and the {2} selected Warehouse(s). Continue?",
            [existing_rows, frm.doc.branch, warehouses.length]
        )
        : __(
            "The inventory detail table will be initialized from ERP stock for Branch {0} and the {1} selected Warehouse(s). Any existing detail data will be cleared. Continue?",
            [frm.doc.branch, warehouses.length]
        );

    frappe.confirm(warning, () => {
        load_inventory_snapshot(frm, warehouses, detail_field);
    });
}

function load_inventory_snapshot(frm, warehouses, detail_field) {
    frappe.call({
        method: "inventory_campaign.inventory_campaign.doctype.inventory_count_session.inventory_count_session.get_inventory_snapshot",
        args: {
            inventory_campaign: frm.doc.inventory_campaign,
            inventory_date: frm.doc.inventory_date,
            branch: frm.doc.branch,
            warehouses: warehouses,
        },
        freeze: true,
        freeze_message: __("Loading inventory snapshot..."),
        callback(r) {
            if (r.exc) {
                return;
            }

            const rows = r.message || [];

            // Clear only after the server query has succeeded. A server error
            // therefore never destroys an existing count in the browser.
            frm.clear_table(detail_field);

            rows.forEach((data) => {
                const row = frm.add_child(detail_field);

                row.item_group = data.item_group;
                row.item_code = data.item_code;
                row.item_name = data.item_name;
                row.stock_uom = data.stock_uom;
                row.warehouse = data.warehouse;
                row.quality_status = data.quality_status;
                row.stock_account = data.stock_account;
                row.stock_balance = data.stock_balance;
                row.ct_conversion_factor = data.ct_conversion_factor;
                row.qty_in_ct = data.qty_in_ct;
                row.valuation_rate = data.valuation_rate;
                row.stock_valuation = data.stock_valuation;

                // Manual/calculated fields intentionally start clean.
                row.physical_count = null;
                row.quantity_difference = 0;
                row.physical_stock_value = 0;
                row.difference_amount = 0;

                // Keep the client-side values aligned with the server formulas.
                recalculate_inventory_row_values(row);
            });

            frm.set_value("difference_amount", 0);
            frm.refresh_field(detail_field);
            frm.refresh_field("difference_amount");
            frm.dirty();

            frappe.show_alert({
                message: __("{0} inventory line(s) loaded. Save the session to persist them.", [rows.length]),
                indicator: "green",
            });
        },
    });
}

function recalculate_inventory_row(frm, cdt, cdn) {
    const row = locals[cdt][cdn];
    recalculate_inventory_row_values(row);

    const detail_field = get_inventory_detail_field(frm);
    frm.refresh_field(detail_field);
    recalculate_header_difference_amount(frm);
}

function recalculate_inventory_row_values(row) {
    const qty_in_ct = to_number(row.qty_in_ct);
    const stock_valuation = to_number(row.stock_valuation);

    // Business rule: valuation rate is the ERP stock value per CT.
    // If there is no theoretical CT quantity, the ratio cannot be derived
    // client-side. Keep the existing rate (the server remains authoritative).
    if (qty_in_ct !== 0) {
        row.valuation_rate = stock_valuation / qty_in_ct;
    } else {
        row.valuation_rate = to_number(row.valuation_rate);
    }

    // A blank Physical Count means that the line has not been counted yet.
    // It must not be interpreted as a physical quantity of zero.
    if (is_blank(row.physical_count)) {
        row.quantity_difference = 0;
        row.physical_stock_value = 0;
        row.difference_amount = 0;
        return;
    }

    const physical_count = to_number(row.physical_count);
    const valuation_rate = to_number(row.valuation_rate);

    row.quantity_difference = physical_count - qty_in_ct;
    row.physical_stock_value = physical_count * valuation_rate;
    row.difference_amount = row.physical_stock_value - stock_valuation;
}

function recalculate_header_difference_amount(frm) {
    const detail_field = get_inventory_detail_field(frm);
    const rows = frm.doc[detail_field] || [];

    let total = 0;
    rows.forEach((row) => {
        // Defensive recalculation keeps the header correct even after grid edits.
        recalculate_inventory_row_values(row);
        total += to_number(row.difference_amount);
    });

    frm.set_value("difference_amount", total);
    frm.refresh_field(detail_field);
    frm.refresh_field("difference_amount");
}

function is_blank(value) {
    return value === null || value === undefined || value === "";
}

function to_number(value) {
    const parsed = Number.parseFloat(value);
    return Number.isFinite(parsed) ? parsed : 0;
}

function get_selected_warehouses(frm) {
    const rows = frm.doc.inventory_count_warehouses || [];
    if (!rows.length) {
        return [];
    }

    // Table MultiSelect normally contains a Link field named `warehouse`.
    // Discover the actual Warehouse Link dynamically so the button remains
    // valid even if the child field was given another name.
    let warehouse_field = "warehouse";
    const table_df = frappe.meta.get_docfield(
        frm.doctype,
        "inventory_count_warehouses",
        frm.doc.name
    );

    if (table_df && table_df.options) {
        const child_meta = frappe.get_meta(table_df.options);
        const warehouse_link = (child_meta.fields || []).find(
            (df) => df.fieldtype === "Link" && df.options === "Warehouse"
        );

        if (warehouse_link) {
            warehouse_field = warehouse_link.fieldname;
        }
    }

    return [...new Set(
        rows
            .map((row) => row[warehouse_field] || row.warehouse)
            .filter(Boolean)
    )];
}

function get_inventory_detail_field(frm) {
    const detail_df = (frm.meta.fields || []).find(
        (df) =>
            df.fieldtype === "Table" &&
            df.options === "Inventory Count Session Detail"
    );

    return detail_df ? detail_df.fieldname : "table_uawl";
}
