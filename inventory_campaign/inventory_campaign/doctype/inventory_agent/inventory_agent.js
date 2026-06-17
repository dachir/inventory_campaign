// Copyright (c) 2026, Richard Amouzou and contributors
// For license information, please see license.txt

frappe.ui.form.on('Inventory Agent', {
  refresh(frm) {
    if (frm.is_new()) return;

    frm.add_custom_button(__('Generate / Regenerate Agent Token'), () => {
      openInventoryAgentTokenValidityDialog(frm);
    }, __('Mobile Access'));

    if (frm.doc.token_status === 'Active' && frm.doc.agent_token_hash) {
      frm.add_custom_button(__('Show Mobile QR'), () => {
        showInventoryAgentQr(frm);
      }, __('Mobile Access'));

      frm.add_custom_button(__('Disable Token'), () => {
        disableInventoryAgentToken(frm);
      }, __('Mobile Access'));
    }

    if (frm.doc.bound_device_id) {
      frm.add_custom_button(__('Reset Device Binding'), () => {
        resetInventoryAgentDeviceBinding(frm);
      }, __('Mobile Access'));
    }
  },
});

function openInventoryAgentTokenValidityDialog(frm) {
  const defaultFrom = formatInventoryAgentDateTime(new Date());
  const defaultUntilDate = new Date();
  defaultUntilDate.setDate(defaultUntilDate.getDate() + 30);
  const defaultUntil = formatInventoryAgentDateTime(defaultUntilDate);

  const dialog = new frappe.ui.Dialog({
    title: __('Generate Mobile Access QR'),
    fields: [
      {
        fieldtype: 'Datetime',
        fieldname: 'valid_from',
        label: __('Start Date'),
        reqd: 1,
        default: defaultFrom,
      },
      {
        fieldtype: 'Datetime',
        fieldname: 'valid_until',
        label: __('End Date'),
        reqd: 1,
        default: defaultUntil,
      },
      {
        fieldtype: 'HTML',
        fieldname: 'help',
        options: `
          <div class="text-muted" style="margin-top:8px;">
            ${__('The agent will be able to scan the QR only within this validity window. Regenerating the token invalidates the previous one.')}
          </div>
        `,
      },
    ],
    primary_action_label: __('Generate QR'),
    primary_action(values) {
      if (!values.valid_from || !values.valid_until) {
        frappe.msgprint(__('Start Date and End Date are required.'));
        return;
      }

      dialog.hide();
      generateInventoryAgentToken(frm, values.valid_from, values.valid_until);
    },
  });

  dialog.show();
}

function generateInventoryAgentToken(frm, validFrom, validUntil) {
  frappe.confirm(
    __('Generate a new mobile token for this Inventory Agent? The previous token will stop working.'),
    () => {
      frappe.call({
        method: 'inventory_campaign.api.agent.generate_agent_token',
        args: {
          inventory_agent: frm.doc.name,
          valid_from: validFrom,
          valid_until: validUntil,
        },
        freeze: true,
        freeze_message: __('Generating QR...'),
        callback(r) {
          const message = r.message || {};
          if (!message.ok) {
            frappe.msgprint({
              title: __('Token generation failed'),
              indicator: 'red',
              message: frappe.utils.escape_html(message.reason || __('Unknown error')),
            });
            return;
          }

          const showDialog = () => showInventoryAgentTokenDialog(message, true);
          const reload = frm.reload_doc();
          if (reload && reload.then) {
            reload.then(showDialog);
          } else {
            showDialog();
          }
        },
      });
    }
  );
}

function showInventoryAgentQr(frm) {
  frappe.call({
    method: 'inventory_campaign.api.agent.get_agent_token_qr_payload',
    args: {
      inventory_agent: frm.doc.name,
    },
    freeze: true,
    freeze_message: __('Loading QR...'),
    callback(r) {
      const message = r.message || {};
      if (!message.ok) {
        frappe.msgprint({
          title: __('QR unavailable'),
          indicator: 'red',
          message: frappe.utils.escape_html(message.reason || __('Unknown error')),
        });
        return;
      }

      showInventoryAgentTokenDialog(message, false);
    },
  });
}

function disableInventoryAgentToken(frm) {
  frappe.confirm(
    __('Disable this Inventory Agent token? The mobile app will no longer be able to connect with it.'),
    () => {
      frappe.call({
        method: 'inventory_campaign.api.agent.disable_agent_token',
        args: {
          inventory_agent: frm.doc.name,
        },
        freeze: true,
        freeze_message: __('Disabling token...'),
        callback(r) {
          const message = r.message || {};
          if (message.ok) {
            frappe.show_alert({ message: __('Token disabled.'), indicator: 'green' });
            frm.reload_doc();
          }
        },
      });
    }
  );
}

function resetInventoryAgentDeviceBinding(frm) {
  frappe.confirm(
    __('Reset the mobile device binding for this Inventory Agent?'),
    () => {
      frappe.call({
        method: 'inventory_campaign.api.agent.reset_agent_device_binding',
        args: {
          inventory_agent: frm.doc.name,
        },
        freeze: true,
        freeze_message: __('Resetting device binding...'),
        callback(r) {
          const message = r.message || {};
          if (message.ok) {
            frappe.show_alert({ message: __('Device binding reset.'), indicator: 'green' });
            frm.reload_doc();
          }
        },
      });
    }
  );
}

function showInventoryAgentTokenDialog(message, tokenWasJustGenerated) {
  const qrPayloadJson = message.qr_payload_json || JSON.stringify(message.qr_payload || {}, null, 2);
  const qrPngDataUrl = message.qr_png_data_url || '';
  const validFrom = message.token_valid_from || '';
  const validUntil = message.token_valid_until || '';

  const generationAlert = tokenWasJustGenerated
    ? `<div class="alert alert-info" style="margin-bottom:12px;">
        ${__('The QR below contains only the ERPNext reachable URL and the one-time agent token. Ask the mobile user to scan it now. Do not send this QR through an unsecured channel.')}
       </div>`
    : '';

  const qrBlock = qrPngDataUrl
    ? `<div style="text-align:center;margin:12px 0;">
         <img src="${qrPngDataUrl}" alt="Inventory Agent QR" style="width:280px;height:280px;image-rendering:pixelated;border:1px solid var(--border-color);border-radius:8px;padding:8px;background:#fff;">
       </div>`
    : `<div class="alert alert-orange" style="margin-bottom:12px;">
         ${__('QR image generation is not available on the server. Install the Python qrcode package, or copy the JSON payload and generate the QR manually.')}
       </div>`;

  const html = `
    <div>
      ${generationAlert}
      <p class="text-muted">
        ${__('Open the Inventory Campaign mobile app and scan this QR to save the mobile credential on the device.')}
      </p>

      ${qrBlock}

      <div class="row" style="margin-bottom:12px;">
        <div class="col-sm-6">
          <label class="control-label">${__('Start Date')}</label>
          <div>${frappe.utils.escape_html(validFrom)}</div>
        </div>
        <div class="col-sm-6">
          <label class="control-label">${__('End Date')}</label>
          <div>${frappe.utils.escape_html(validUntil)}</div>
        </div>
      </div>

      <details>
        <summary>${__('Minimal QR Payload')}</summary>
        <textarea class="form-control" id="inventory-agent-qr-json" readonly rows="8" style="margin-top:8px;">${frappe.utils.escape_html(qrPayloadJson)}</textarea>
      </details>
    </div>
  `;

  const dialog = new frappe.ui.Dialog({
    title: tokenWasJustGenerated ? __('Mobile QR Generated') : __('Mobile Connection QR'),
    size: 'large',
    fields: [
      {
        fieldtype: 'HTML',
        fieldname: 'token_html',
        options: html,
      },
    ],
    primary_action_label: __('Close'),
    primary_action() {
      dialog.hide();
    },
  });

  dialog.show();

  dialog.set_secondary_action_label(__('Copy QR JSON'));
  dialog.set_secondary_action(() => copyInventoryAgentText('#inventory-agent-qr-json'));
}

function copyInventoryAgentText(selector) {
  const element = document.querySelector(selector);
  if (!element) return;

  element.select();
  element.setSelectionRange(0, 999999);

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(element.value).then(() => {
      frappe.show_alert({ message: __('Copied.'), indicator: 'green' });
    });
    return;
  }

  document.execCommand('copy');
  frappe.show_alert({ message: __('Copied.'), indicator: 'green' });
}

function formatInventoryAgentDateTime(date) {
  const pad = (value) => String(value).padStart(2, '0');
  return [
    date.getFullYear(),
    pad(date.getMonth() + 1),
    pad(date.getDate()),
  ].join('-') + ' ' + [
    pad(date.getHours()),
    pad(date.getMinutes()),
    pad(date.getSeconds()),
  ].join(':');
}
