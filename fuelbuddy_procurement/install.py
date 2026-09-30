"""Install-time setup: the Purchase Receipt Item field a row names its advance in, disabling the DB
Server Scripts this app replaces so their logic does not run twice, and the advance quantities on
PARCs saved before those fields existed.

``after_install`` does all three; ``after_migrate`` re-applies the field and the quantities on sites
that already have the app. (A patch would not do: ``bench install-app`` marks a new app's patches as
already applied instead of executing them.)
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import flt

from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control import (
	PARC,
	PARC_FIELD,
	consumed_qty,
	remaining_qty,
)

CUSTOM_FIELDS = {
	"Purchase Receipt Item": [
		{
			"fieldname": PARC_FIELD,
			"fieldtype": "Link",
			"options": PARC,
			"label": "Advance (PARC)",
			"insert_after": "purchase_order",
			# Duplicates and returns start without an advance.
			"no_copy": 1,
			"print_hide": 1,
			# The picker offers only open advances on this row's Purchase Order.
			"link_filters": json.dumps(
				[[PARC, "docstatus", "=", 0], [PARC, "purchase_order", "=", "eval:doc.purchase_order"]]
			),
			"description": "The advance this row books against: only the supplier's oldest advance with "
			"quantity left, on one row, for at most what it has left. Submitting the receipt books the "
			"row's quantity against it.",
		}
	]
}

# The three legacy prod scripts plus the two cancel scripts in db_scripts/ (same behaviour as this
# app, for sites that cannot take the app yet). Only one of the two mechanisms may be enabled.
LEGACY_SERVER_SCRIPTS = [
	"Purchase Advance Receipt Control -  Warning",
	"Purchase Advance Receipt Control Payment Entry",
	"Purchase Advance Receipt Control Payment Entry Cancel",
	"Purchase Advance Receipt Control Purchase Receipt",
	"Purchase Advance Receipt Control Purchase Receipt Cancel",
]


def after_install():
	create_parc_fields()
	for name in LEGACY_SERVER_SCRIPTS:
		if frappe.db.exists("Server Script", name):
			frappe.db.set_value("Server Script", name, "disabled", 1)
	frappe.cache.delete_value("server_script_map")
	fill_advance_quantities()


def after_migrate():
	create_parc_fields()
	fill_advance_quantities()


def create_parc_fields():
	create_custom_fields(CUSTOM_FIELDS)


def fill_advance_quantities():
	"""Sets qty_consumed and qty_remaining on open advances saved before those fields existed. They are
	shown on the form and in the list; the receipt checks compute both from the consumption rows.
	Idempotent: writes only where a stored value differs."""
	for name in frappe.get_all(PARC, filters={"docstatus": 0}, pluck="name"):
		parc = frappe.get_doc(PARC, name)
		values = {"qty_consumed": consumed_qty(parc), "qty_remaining": remaining_qty(parc)}
		if any(flt(parc.get(field), 6) != flt(value, 6) for field, value in values.items()):
			parc.db_set(values, update_modified=False)
