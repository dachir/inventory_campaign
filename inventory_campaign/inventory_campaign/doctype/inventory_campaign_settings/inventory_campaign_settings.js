// Copyright (c) 2026, Richard Amouzou and contributors
// For license information, please see license.txt

frappe.ui.form.on("Inventory Campaign Settings", {
    refresh(frm) {
        set_server_reachable_url(frm);
    },

    protocol(frm) {
        set_server_reachable_url(frm);
    },

    server_url(frm) {
        set_server_reachable_url(frm);
    },

    before_save(frm) {
        set_server_reachable_url(frm);
    }
});

function set_server_reachable_url(frm) {
    let protocole = (frm.doc.protocol || "http").trim();
    let server_url = (frm.doc.server_url || "").trim();

    protocole = protocole
        .replace("://", "")
        .replace("/", "")
        .toLowerCase();

    if (!["http", "https"].includes(protocole)) {
        protocole = "http";
    }

    server_url = server_url
        .replace(/^https?:\/\//i, "")
        .replace(/\/+$/g, "")
        .trim();

    if (!server_url) {
        frm.set_value("server_reachable_url", "");
        return;
    }

    const reachable_url = `${protocole}://${server_url}`;

    if (frm.doc.server_reachable_url !== reachable_url) {
        frm.set_value("server_reachable_url", reachable_url);
    }
}
