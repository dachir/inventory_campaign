// apps/inventory_campaign/inventory_campaign/public/js/stock_reconciliation.js

frappe.ui.form.on('Stock Reconciliation', {
  refresh(frm) {
    if (frm.is_new()) return;
    if (frm.doc.docstatus !== 0) return;

    frm.add_custom_button(__('Import Inventory Sessions'), () => {
      showInventorySessionImportDialog(frm);
    }, __('Inventory Campaign'));
  },
});

function showInventorySessionImportDialog(frm) {
  let loadedSessions = [];

  const dialog = new frappe.ui.Dialog({
    title: __('Import Inventory Sessions'),
    size: 'extra-large',
    fields: [
      {
        fieldtype: 'HTML',
        fieldname: 'intro_html',
        options: `<p class="text-muted">${__('Select submitted Inventory Sessions. Sessions with unplanned discoveries or recoding proposals must be reviewed before import. Stock Reconciliation will remain draft.')}</p>`,
      },
      {
        fieldtype: 'Link',
        fieldname: 'campaign',
        label: __('Inventory Campaign'),
        options: 'Inventory Campaign',
        default: frm.doc.custom_inventory_campaign || '',
      },
      {
        fieldtype: 'Column Break',
      },
      {
        fieldtype: 'Link',
        fieldname: 'warehouse',
        label: __('Warehouse'),
        options: 'Warehouse',
        default: frm.doc.set_warehouse || frm.doc.warehouse || '',
      },
      {
        fieldtype: 'Section Break',
      },
      {
        fieldtype: 'Link',
        fieldname: 'inventory_agent',
        label: __('Inventory Agent'),
        options: 'Inventory Agent',
      },
      {
        fieldtype: 'Column Break',
      },
      {
        fieldtype: 'Check',
        fieldname: 'strict_review',
        label: __('Require review for anomaly/recoding sessions'),
        default: 1,
      },
      {
        fieldtype: 'Section Break',
      },
      {
        fieldtype: 'HTML',
        fieldname: 'sessions_html',
        options: `<div class="text-muted">${__('Click Load Sessions to search.')}</div>`,
      },
    ],
    primary_action_label: __('Import Selected'),
    primary_action(values) {
      const selected = getSelectedInventorySessions(dialog);
      if (!selected.length) {
        frappe.msgprint(__('Select at least one Inventory Session.'));
        return;
      }

      frappe.confirm(
        __('Import {0} Inventory Session(s) into this draft Stock Reconciliation?', [selected.length]),
        () => {
          frappe.call({
            method: 'inventory_campaign.api.stock_reconciliation.import_inventory_sessions',
            args: {
              stock_reconciliation: frm.doc.name,
              inventory_sessions: selected,
              strict_review: values.strict_review ? 1 : 0,
              merge_existing_rows: 1,
            },
            freeze: true,
            freeze_message: __('Importing Inventory Sessions...'),
            callback(r) {
              if (!r.message) return;
              if (!r.message.ok) {
                frappe.msgprint({
                  title: __('Import failed'),
                  indicator: 'red',
                  message: renderErrors(r.message.errors || [r.message.reason || __('Unknown error')]),
                });
                return;
              }

              frappe.show_alert({
                message: __('Inventory Sessions imported. Review Stock Reconciliation before submitting.'),
                indicator: 'green',
              });
              dialog.hide();
              frm.reload_doc();
            },
          });
        }
      );
    },
  });

  dialog.set_secondary_action_label(__('Load Sessions'));
  dialog.set_secondary_action(() => {
    const values = dialog.get_values() || {};
    frappe.call({
      method: 'inventory_campaign.api.stock_reconciliation.get_importable_inventory_sessions',
      args: {
        campaign: values.campaign || null,
        warehouse: values.warehouse || null,
        inventory_agent: values.inventory_agent || null,
        strict_review: values.strict_review ? 1 : 0,
        limit_page_length: 100,
      },
      freeze: true,
      freeze_message: __('Loading Inventory Sessions...'),
      callback(r) {
        loadedSessions = (r.message && r.message.sessions) || [];
        dialog.fields_dict.sessions_html.$wrapper.html(renderInventorySessionTable(loadedSessions));
      },
    });
  });

  dialog.show();
}

function getSelectedInventorySessions(dialog) {
  const selected = [];
  dialog.fields_dict.sessions_html.$wrapper.find('input[data-inventory-session]:checked').each(function () {
    selected.push($(this).attr('data-inventory-session'));
  });
  return selected;
}

function renderInventorySessionTable(rows) {
  if (!rows || !rows.length) {
    return `<div class="text-muted">${__('No submitted Inventory Session found for these filters.')}</div>`;
  }

  const body = rows.map((row) => {
    const disabled = row.importable ? '' : 'disabled';
    const indicator = row.importable ? 'green' : 'red';
    const review = row.review_status || 'Pending';
    const problems = [];

    if (row.errors && row.errors.length) problems.push(...row.errors);
    if (row.warnings && row.warnings.length) problems.push(...row.warnings);

    const problemHtml = problems.length
      ? `<div class="small text-muted" style="max-width:360px;">${frappe.utils.escape_html(problems.join(' | '))}</div>`
      : '';

    return `
      <tr>
        <td><input type="checkbox" data-inventory-session="${frappe.utils.escape_html(row.name)}" ${disabled}></td>
        <td><span class="indicator ${indicator}">${frappe.utils.escape_html(row.name)}</span>${problemHtml}</td>
        <td>${frappe.utils.escape_html(row.campaign || '')}</td>
        <td>${frappe.utils.escape_html(row.inventory_agent || '')}</td>
        <td>${frappe.utils.escape_html(row.warehouse || row.parent_warehouse || '')}</td>
        <td class="text-right">${row.total_items_counted || 0}</td>
        <td class="text-right">${row.total_qty_counted || 0}</td>
        <td>${frappe.utils.escape_html(review)}</td>
      </tr>
    `;
  }).join('');

  return `
    <div style="max-height:420px;overflow:auto;border:1px solid var(--border-color);border-radius:8px;">
      <table class="table table-bordered table-hover" style="margin:0;">
        <thead>
          <tr>
            <th style="width:40px;"></th>
            <th>${__('Session')}</th>
            <th>${__('Campaign')}</th>
            <th>${__('Agent')}</th>
            <th>${__('Warehouse')}</th>
            <th class="text-right">${__('Items')}</th>
            <th class="text-right">${__('Qty')}</th>
            <th>${__('Review')}</th>
          </tr>
        </thead>
        <tbody>${body}</tbody>
      </table>
    </div>
  `;
}

function renderErrors(errors) {
  const list = (errors || []).map((error) => `<li>${frappe.utils.escape_html(String(error))}</li>`).join('');
  return `<ul>${list}</ul>`;
}
