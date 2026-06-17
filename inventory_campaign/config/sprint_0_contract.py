"""Sprint 0 contract for Inventory Campaign v2.

This module is intentionally side-effect free. It gives the next coding sprints
an explicit, versioned contract without creating or migrating any ERPNext data.

Sprint 0 scope:
- clarify the corrected functional model;
- freeze planned/unplanned rules;
- prepare Sprint 1 implementation work;
- do not create DocTypes from this file.
"""

CONTRACT_VERSION = "inventory_campaign_v2_sprint_0"
TARGET_SITE = "erpv15dev.marsavco.com"

PRODUCT_PRINCIPLE = (
	"Le mobile compte. ERPNext reçoit et contrôle. "
	"Stock Reconciliation corrige officiellement."
)

DO_NOT_CREATE_AUTOMATICALLY = [
	"Item",
	"Warehouse",
	"Item Group",
	"Category",
]

CORE_DOCTYPES = {
	"inventory_campaign": {
		"doctype": "Inventory Campaign",
		"purpose": "Organiser une campagne d'inventaire liée à un dépôt parent.",
		"minimum_fields": [
			"campaign_name",
			"company",
			"warehouse",
			"status",
			"start_date",
			"end_date",
			"responsible_user",
			"notes",
		],
	},
	"inventory_agent": {
		"doctype": "Inventory Agent",
		"purpose": "Représenter le compteur terrain sans le mélanger avec ERPNext User.",
		"minimum_fields": [
			"agent_code",
			"agent_name",
			"status",
			"company",
			"phone",
			"email",
			"authorized_locations",
			"authorized_items",
			"access_token",
			"access_token_hash",
			"token_status",
			"token_valid_from",
			"token_valid_until",
			"mobile_credential_ttl",
			"bind_to_first_device",
			"bound_device_id",
			"bound_at",
			"last_token_used_at",
		],
	},
	"inventory_session": {
		"doctype": "Inventory Session",
		"purpose": "Stocker la session soumise par le mobile après comptage.",
		"minimum_fields": [
			"campaign",
			"inventory_agent",
			"mobile_session_id",
			"company",
			"parent_warehouse",
			"location_warehouse",
			"status",
			"opened_at",
			"closed_at",
			"submitted_at",
			"server_ack_at",
			"items",
			"unplanned_items_json",
			"unplanned_warehouses_json",
			"raw_payload_json",
			"has_unplanned_items",
			"unplanned_items_count",
			"has_unplanned_warehouses",
			"unplanned_warehouses_count",
			"review_status",
			"reviewed_by",
			"reviewed_at",
			"review_note",
		],
	},
	"inventory_session_item": {
		"doctype": "Inventory Session Item",
		"purpose": "Stocker les lignes normales prévues dans le périmètre de l'agent.",
		"minimum_fields": [
			"item_code",
			"item_name",
			"barcode",
			"uom",
			"counted_qty",
			"scan_count",
			"last_scanned_at",
			"manual_entry",
			"notes",
		],
	},
}

PLANNED_UNPLANNED_RULES = {
	"item_scan": {
		"authorized_item": "Create normal Inventory Session Item row.",
		"known_but_not_authorized_item": "Store evidence in unplanned_items_json.",
		"unknown_item": "Store scan_code/raw evidence in unplanned_items_json.",
	},
	"warehouse_or_location_scan": {
		"authorized_location": "Continue normal session flow.",
		"known_but_not_authorized_location": "Store evidence in unplanned_warehouses_json.",
		"unknown_location": "Store scan_code/raw evidence in unplanned_warehouses_json.",
	},
}

UNPLANNED_JSON_SCHEMAS = {
	"unplanned_items_json": {
		"type": "array",
		"items": {
			"scan_code": "string|null",
			"item_code": "string|null",
			"item_name": "string|null",
			"barcode": "string|null",
			"counted_qty": "number",
			"uom": "string|null",
			"location_warehouse": "string|null",
			"raw_location_code": "string|null",
			"reason": "string",
			"detected_at": "datetime string",
			"mobile_session_id": "string",
		},
	},
	"unplanned_warehouses_json": {
		"type": "array",
		"items": {
			"scan_code": "string|null",
			"warehouse": "string|null",
			"warehouse_name": "string|null",
			"raw_location_name": "string|null",
			"parent_warehouse": "string|null",
			"reason": "string",
			"detected_at": "datetime string",
			"mobile_session_id": "string",
		},
	},
}

API_CONTRACTS = {
	"validate_agent_access_token": {
		"creates_erpnext_session": False,
		"returns": [
			"inventory_agent",
			"authorized_locations",
			"authorized_items",
			"available_campaigns",
			"mobile_credential",
			"credential_expires_at",
		],
	},
	"get_inventory_context": {
		"requires": ["mobile_credential"],
		"returns": [
			"inventory_agent",
			"authorized_locations",
			"authorized_items",
			"available_campaigns",
		],
	},
	"submit_inventory_session": {
		"requires": [
			"mobile_credential",
			"mobile_session_id",
			"campaign",
			"inventory_agent",
			"items",
			"unplanned_items",
			"unplanned_warehouses",
		],
		"server_rules": [
			"Revalidate mobile credential.",
			"Verify inventory_agent.",
			"Ensure mobile_session_id + inventory_agent uniqueness.",
			"Create Inventory Session.",
			"Create Inventory Session Item rows for normal counted items.",
			"Store unplanned_items in unplanned_items_json.",
			"Store unplanned_warehouses in unplanned_warehouses_json.",
			"Compute review flags and counts.",
			"Return server ACK.",
		],
	},
}

SPRINT_PLAN = [
	{
		"sprint": "Sprint 0",
		"duration": "2-3 jours",
		"objective": "Cadrage corrigé",
		"expected_result": "Contrat v2 validé",
	},
	{
		"sprint": "Sprint 1",
		"duration": "1 semaine",
		"objective": "Modèle ERPNext",
		"expected_result": "Inventory Agent + sessions + champs JSON",
	},
	{
		"sprint": "Sprint 2",
		"duration": "1 semaine",
		"objective": "Token agent",
		"expected_result": "QR + mobile credential",
	},
	{
		"sprint": "Sprint 3",
		"duration": "1 semaine",
		"objective": "Contexte mobile",
		"expected_result": "Campagnes, articles, emplacements",
	},
	{
		"sprint": "Sprint 4",
		"duration": "1 semaine",
		"objective": "Scan planned/unplanned",
		"expected_result": "Comptage structuré + anomalies JSON",
	},
	{
		"sprint": "Sprint 5",
		"duration": "1 semaine",
		"objective": "Submit + purge",
		"expected_result": "Session envoyée, mobile vidé après ACK",
	},
	{
		"sprint": "Sprint 6",
		"duration": "1 semaine",
		"objective": "Revue + Stock Reconciliation",
		"expected_result": "Import contrôlé",
	},
	{
		"sprint": "Sprint 7",
		"duration": "1 semaine",
		"objective": "Sécurité + tests terrain",
		"expected_result": "MVP pilote sécurisé",
	},
]

DEFINITION_OF_DONE = [
	"Le contrat fonctionnel v2 est écrit.",
	"Les règles Inventory Agent sont figées.",
	"Les règles authorized/unplanned sont figées.",
	"Le stockage JSON des anomalies est retenu.",
	"La non-création automatique de master data est retenue.",
	"Le backlog Sprint 1 à Sprint 7 est réaligné.",
	"Les critères d'acceptation Sprint 1 sont suffisamment clairs pour coder.",
]
