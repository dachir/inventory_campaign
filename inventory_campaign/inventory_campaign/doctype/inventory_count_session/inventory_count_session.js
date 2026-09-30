frappe.ui.form.on("Inventory Count Session", {
    refresh(frm) {
        setup_manual_valuation_grid_ui(frm);

        if (frm.doc.docstatus !== 0) {
            return;
        }

        frm.add_custom_button(__("Initialize Inventory"), () => {
            initialize_inventory(frm);
        });

        frm.add_custom_button(__("Add Manual Entry"), () => {
            add_manual_inventory_entry(frm);
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

    valuation_rate(frm, cdt, cdn) {
        const row = locals[cdt][cdn];

        // valuation_rate is manually editable only when ERP has no historical
        // valuation and this is not a fully manual inventory row.
        if (cint(row.manual_valuation_required) && !cint(row.manual_entry)) {
            recalculate_inventory_row(frm, cdt, cdn);
        }
    },

    physical_stock_value(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (cint(row.manual_entry)) {
            recalculate_inventory_row(frm, cdt, cdn);
        }
    },

    manual_entry(frm, cdt, cdn) {
        refresh_manual_valuation_grid_rows(frm);
    },

    manual_valuation_required(frm, cdt, cdn) {
        refresh_manual_valuation_grid_rows(frm);
    },

    form_render(frm, cdt, cdn) {
        refresh_manual_valuation_grid_rows(frm);
    },

    table_uawl_remove(frm) {
        recalculate_header_difference_amount(frm);
    },
});

function add_manual_inventory_entry(frm) {
    const warehouses = get_selected_warehouses(frm);

    if (!frm.doc.inventory_campaign || !frm.doc.inventory_date || !frm.doc.branch) {
        frappe.msgprint({
            title: __("Missing Session Information"),
            message: __("Select Inventory Campaign, Inventory Date and Branch before adding a manual entry."),
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

    const dialog = new frappe.ui.Dialog({
        title: __("Add Manual Inventory Entry"),
        fields: [
            {
                fieldname: "item_code",
                fieldtype: "Link",
                options: "Item",
                label: __("Item"),
                reqd: 1,
            },
            {
                fieldname: "warehouse",
                fieldtype: "Link",
                options: "Warehouse",
                label: __("Warehouse"),
                reqd: 1,
            },
            {
                fieldname: "quality_status",
                fieldtype: "Link",
                options: "Quality Status",
                label: __("Quality Status"),
                default: "A",
                reqd: 1,
            },
            { fieldtype: "Section Break" },
            {
                fieldname: "physical_count",
                fieldtype: "Float",
                label: __("Physical Count (CT)"),
                reqd: 1,
            },
            {
                fieldname: "physical_stock_value",
                fieldtype: "Currency",
                label: __("Physical Stock Value"),
                reqd: 1,
            },
        ],
        primary_action_label: __("Add Entry"),
        primary_action(values) {
            const physical_count = to_number(values.physical_count);
            const physical_stock_value = to_number(values.physical_stock_value);

            if (physical_count <= 0) {
                frappe.msgprint(__("Physical Count must be greater than zero for a manual entry."));
                return;
            }

            if (physical_stock_value < 0) {
                frappe.msgprint(__("Physical Stock Value cannot be negative."));
                return;
            }

            const detail_field = get_inventory_detail_field(frm);
            const duplicate = (frm.doc[detail_field] || []).find((row) =>
                row.item_code === values.item_code &&
                row.warehouse === values.warehouse &&
                (row.quality_status || "A") === (values.quality_status || "A")
            );

            if (duplicate) {
                frappe.msgprint({
                    title: __("Inventory Line Already Exists"),
                    message: __(
                        "Item {0} already exists for Warehouse {1} and Quality Status {2}. Modify the existing line instead.",
                        [values.item_code, values.warehouse, values.quality_status || "A"]
                    ),
                    indicator: "orange",
                });
                return;
            }

            frappe.call({
                method: "inventory_campaign.inventory_campaign.doctype.inventory_count_session.inventory_count_session.get_manual_inventory_entry_defaults",
                args: {
                    inventory_campaign: frm.doc.inventory_campaign,
                    inventory_date: frm.doc.inventory_date,
                    branch: frm.doc.branch,
                    item_code: values.item_code,
                    warehouse: values.warehouse,
                    quality_status: values.quality_status || "A",
                    selected_warehouses: warehouses,
                    inventory_count_session: frm.is_new() ? null : frm.doc.name,
                },
                freeze: true,
                freeze_message: __("Preparing manual inventory entry..."),
                callback(r) {
                    if (r.exc || !r.message) {
                        return;
                    }

                    const data = r.message;
                    const row = frm.add_child(detail_field);

                    row.item_group = data.item_group;
                    row.item_code = data.item_code;
                    row.item_name = data.item_name;
                    row.stock_uom = data.stock_uom;
                    row.warehouse = data.warehouse;
                    row.quality_status = data.quality_status;
                    row.stock_account = data.stock_account;
                    row.stock_balance = 0;
                    row.ct_conversion_factor = data.ct_conversion_factor;
                    row.qty_in_ct = 0;
                    row.stock_valuation = 0;
                    row.physical_count = physical_count;
                    row.physical_stock_value = physical_stock_value;
                    row.valuation_rate = physical_count
                        ? physical_stock_value / physical_count
                        : 0;
                    row.quantity_difference = physical_count;
                    row.difference_amount = physical_stock_value;
                    row.manual_valuation_required = 0;
                    row.manual_entry = 1;

                    dialog.hide();
                    frm.refresh_field(detail_field);
                    recalculate_header_difference_amount(frm);
                    refresh_manual_valuation_grid_rows(frm);
                    frm.dirty();

                    frappe.show_alert({
                        message: __("Manual inventory entry added."),
                        indicator: "orange",
                    });
                },
            });
        },
    });

    dialog.fields_dict.item_code.get_query = () => ({
        filters: {
            is_stock_item: 1,
            disabled: 0,
        },
    });

    dialog.fields_dict.warehouse.get_query = () => ({
        filters: {
            name: ["in", warehouses],
        },
    });

    dialog.show();
}

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
            inventory_count_session: frm.is_new() ? null : frm.doc.name,
        },
        freeze: true,
        freeze_message: __("Loading inventory snapshot..."),
        callback(r) {
            if (r.exc) {
                return;
            }

            const rows = r.message || [];

            // Clear only after the server query succeeds.
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
                row.manual_valuation_required = cint(data.manual_valuation_required);
                row.manual_entry = 0;
                row.stock_valuation = data.stock_valuation;

                // Initialization is deliberately neutral: the physical values start
                // equal to the ERP snapshot and only user changes create a variance.
                row.physical_count = data.qty_in_ct;
                row.quantity_difference = 0;
                row.physical_stock_value = data.stock_valuation;
                row.difference_amount = 0;
            });

            frm.set_value("difference_amount", 0);
            frm.refresh_field(detail_field);
            frm.refresh_field("difference_amount");
            frm.dirty();

            setup_manual_valuation_grid_ui(frm);
            refresh_manual_valuation_grid_rows(frm);

            const manual_count = rows.filter(
                (row) => cint(row.manual_valuation_required)
            ).length;

            frappe.show_alert({
                message: manual_count
                    ? __("{0} line(s) loaded. {1} line(s) require a manual valuation rate.", [rows.length, manual_count])
                    : __("{0} inventory line(s) loaded. Save the session to persist them.", [rows.length]),
                indicator: manual_count ? "orange" : "green",
            });
        },
    });
}

function recalculate_inventory_row(frm, cdt, cdn) {
    const row = locals[cdt][cdn];
    recalculate_inventory_row_values(row);

    const detail_field = get_inventory_detail_field(frm);
    frm.refresh_field(detail_field);
    refresh_manual_valuation_grid_rows(frm);
    recalculate_header_difference_amount(frm);
}

function recalculate_inventory_row_values(row) {
    const qty_in_ct = to_number(row.qty_in_ct);
    const stock_valuation = to_number(row.stock_valuation);
    const manual_required = cint(row.manual_valuation_required) === 1;
    const manual_entry = cint(row.manual_entry) === 1;

    if (manual_entry) {
        const physical_count = is_blank(row.physical_count)
            ? 0
            : to_number(row.physical_count);
        const physical_stock_value = to_number(row.physical_stock_value);

        row.stock_balance = 0;
        row.qty_in_ct = 0;
        row.stock_valuation = 0;
        row.manual_valuation_required = 0;
        row.quantity_difference = physical_count;
        row.valuation_rate = physical_count
            ? physical_stock_value / physical_count
            : 0;
        row.difference_amount = physical_stock_value;
        return;
    }

    if (qty_in_ct !== 0) {
        // Normal ERP valuation: the checkbox must never remain active here.
        row.valuation_rate = stock_valuation / qty_in_ct;
        row.manual_valuation_required = 0;
    } else if (manual_required) {
        // Critical rule: when the checkbox is active, valuation_rate belongs to
        // the user. Never recalculate or clear it client-side.
        row.valuation_rate = to_number(row.valuation_rate);
    } else {
        // qty_in_ct == 0 but the server found a historical rate. Keep exactly
        // that rate. The client must not try to derive it from stock_valuation.
        row.valuation_rate = to_number(row.valuation_rate);
    }

    if (is_blank(row.physical_count)) {
        row.quantity_difference = 0;
        row.physical_stock_value = stock_valuation;
        row.difference_amount = 0;
        return;
    }

    const physical_count = to_number(row.physical_count);
    const valuation_rate = to_number(row.valuation_rate);

    row.quantity_difference = physical_count - qty_in_ct;
    row.difference_amount = row.quantity_difference * valuation_rate;
    row.physical_stock_value = stock_valuation + row.difference_amount;
}

function recalculate_header_difference_amount(frm) {
    const detail_field = get_inventory_detail_field(frm);
    const rows = frm.doc[detail_field] || [];

    let total = 0;
    rows.forEach((row) => {
        recalculate_inventory_row_values(row);
        total += to_number(row.difference_amount);
    });

    frm.set_value("difference_amount", total);
    frm.refresh_field(detail_field);
    refresh_manual_valuation_grid_rows(frm);
    frm.refresh_field("difference_amount");
}

function setup_manual_valuation_grid_ui(frm) {
    ensure_inventory_exception_row_styles();

    // grid-row-render is the reliable place to apply per-row editability.
    // Namespacing avoids stacking duplicate handlers after each refresh.
    $(frm.wrapper)
        .off("grid-row-render.inventory_count_manual_valuation")
        .on("grid-row-render.inventory_count_manual_valuation", (event, grid_row) => {
            if (!is_inventory_detail_grid_row(grid_row)) {
                return;
            }
            apply_manual_valuation_state(grid_row);
        });

    // Also apply to rows already rendered before the event handler was attached.
    setTimeout(() => refresh_manual_valuation_grid_rows(frm), 0);
}

function refresh_manual_valuation_grid_rows(frm) {
    const detail_field = get_inventory_detail_field(frm);
    const grid = frm.fields_dict[detail_field] && frm.fields_dict[detail_field].grid;

    if (!grid || !grid.grid_rows) {
        return;
    }

    grid.grid_rows.forEach((grid_row) => {
        apply_manual_valuation_state(grid_row);
    });
}

function is_inventory_detail_grid_row(grid_row) {
    return Boolean(
        grid_row &&
        grid_row.grid &&
        grid_row.grid.df &&
        grid_row.grid.df.options === "Inventory Count Session Detail"
    );
}

function apply_manual_valuation_state(grid_row) {
    if (!grid_row || !grid_row.doc) {
        return;
    }

    const manual_required = cint(grid_row.doc.manual_valuation_required) === 1;
    const manual_entry = cint(grid_row.doc.manual_entry) === 1;

    // Standard zero-stock rows may unlock valuation_rate when no historical
    // valuation exists. Fully manual rows instead unlock Physical Stock Value;
    // their valuation_rate is derived from value / physical count.
    if (typeof grid_row.toggle_editable === "function") {
        grid_row.toggle_editable("valuation_rate", manual_required && !manual_entry);
        grid_row.toggle_editable("physical_stock_value", manual_entry);
    }

    const $wrapper = grid_row.wrapper;
    if (!$wrapper || !$wrapper.length) {
        return;
    }

    // Put state classes on the actual Grid Row wrapper. Frappe paints the
    // visible cells (.data-row / .grid-static-col) with their own background,
    // so setting only grid_row.wrapper background is not enough.
    $wrapper.toggleClass("inventory-manual-valuation-required", manual_required);
    $wrapper.toggleClass("inventory-manual-entry", manual_entry);

    // Immediate inline fallback. The injected CSS below uses !important and is
    // the authoritative visual rule, but this also makes the state visible
    // immediately before the browser has repainted the grid.
    let background = "";
    let text_color = "";

    if (manual_required) {
        background = "#FFEB9C";
        text_color = "#9C5700";
    } else if (manual_entry) {
        background = "#FFC7CE";
        text_color = "#9C0006";
    }

    $wrapper
        .find(".data-row, .grid-static-col, .row-index, .grid-row-check, .grid-row-open")
        .css({
            "background-color": background,
            "color": text_color
        });

    $wrapper
        .find(".data-row a, .data-row .static-area, .data-row .like-disabled-input, .data-row .form-control, .data-row input, .data-row select, .data-row textarea")
        .css("color", text_color);
}

function ensure_inventory_exception_row_styles() {
    const style_id = "inventory-count-exception-row-styles";
    if (document.getElementById(style_id)) {
        return;
    }

    // Frappe gives the individual grid cells an opaque background. We therefore
    // target the rendered cells, not just the outer .grid-row element.
    const style = document.createElement("style");
    style.id = style_id;
    style.textContent = `
        .grid-row.inventory-manual-entry > .data-row,
        .grid-row.inventory-manual-entry .data-row,
        .grid-row.inventory-manual-entry .grid-static-col,
        .grid-row.inventory-manual-entry .row-index,
        .grid-row.inventory-manual-entry .grid-row-check,
        .grid-row.inventory-manual-entry .grid-row-open {
            background-color: #FFC7CE !important;
            color: #9C0006 !important;
        }

        .grid-row.inventory-manual-entry .data-row a,
        .grid-row.inventory-manual-entry .data-row .static-area,
        .grid-row.inventory-manual-entry .data-row .like-disabled-input,
        .grid-row.inventory-manual-entry .data-row .form-control,
        .grid-row.inventory-manual-entry .data-row input,
        .grid-row.inventory-manual-entry .data-row select,
        .grid-row.inventory-manual-entry .data-row textarea {
            color: #9C0006 !important;
        }

        .grid-row.inventory-manual-valuation-required > .data-row,
        .grid-row.inventory-manual-valuation-required .data-row,
        .grid-row.inventory-manual-valuation-required .grid-static-col,
        .grid-row.inventory-manual-valuation-required .row-index,
        .grid-row.inventory-manual-valuation-required .grid-row-check,
        .grid-row.inventory-manual-valuation-required .grid-row-open {
            background-color: #FFEB9C !important;
            color: #9C5700 !important;
        }

        .grid-row.inventory-manual-valuation-required .data-row a,
        .grid-row.inventory-manual-valuation-required .data-row .static-area,
        .grid-row.inventory-manual-valuation-required .data-row .like-disabled-input,
        .grid-row.inventory-manual-valuation-required .data-row .form-control,
        .grid-row.inventory-manual-valuation-required .data-row input,
        .grid-row.inventory-manual-valuation-required .data-row select,
        .grid-row.inventory-manual-valuation-required .data-row textarea {
            color: #9C5700 !important;
        }
    `;
    document.head.appendChild(style);
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
