// Copyright (c) 2026, Richard Amouzou and contributors
// For license information, please see license.txt

frappe.ui.form.on("Inventory Session", {
    refresh(frm) {
        if (frm.is_new() || frm.doc.docstatus !== 1) {
            return;
        }

        frm.add_custom_button(
            __("Create Inventory Count Session"),
            () => create_inventory_count_session(frm),
            __("Inventory")
        );
    },
});


function create_inventory_count_session(frm) {
    frappe.confirm(
        __(
            "Clean the historical mobile count, preserve its variance against today\'s ERP stock, and create a Draft Inventory Count Session from {0}? Negative adjusted counts will be marked for physical recount.",
            [frm.doc.name]
        ),
        () => {
            frappe.call({
                method: "inventory_campaign.inventory_campaign.doctype.inventory_session.inventory_session.create_inventory_count_session",
                args: {
                    inventory_session: frm.doc.name,
                },
                freeze: true,
                freeze_message: __("Creating Inventory Count Session..."),
                callback(r) {
                    if (r.exc || !r.message) {
                        return;
                    }

                    frappe.show_alert({
                        message: __("Inventory Count Session {0} created in Draft.", [r.message]),
                        indicator: "green",
                    });

                    frappe.set_route("Form", "Inventory Count Session", r.message);
                },
            });
        }
    );
}
