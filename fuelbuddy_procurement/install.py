"""Install-time setup: the Purchase Receipt Item field a row names its advance in and the Purchase
Receipt field a lane receipt names its receipt split hold in, disabling the DB Server Scripts this
app replaces so their logic does not run twice, and the advance quantities on PARCs saved before
those fields existed.

``after_install`` does all three; ``after_migrate`` re-applies the fields and the quantities on sites
that already have the app. (A patch would not do: ``bench install-app`` marks a new app's patches as
already applied instead of executing them.)
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import flt

from fuelbuddy_procurement.allocation import HOLD
from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control import (
	PARC,
	PARC_FIELD,
	consumed_qty,
	remaining_qty,
)
from fuelbuddy_procurement.receipt_hold import HOLD_FIELD

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
			"description": "The advance this row books against: an open advance of the receipt's supplier, "
			"on one row, for at most what it has available (what it has left, less what receipt split "
			"holds hold on it). Submitting the receipt books the row's quantity against it.",
		}
	],
	"Purchase Receipt": [
		{
			"fieldname": HOLD_FIELD,
			"fieldtype": "Link",
			"options": HOLD,
			"label": "Receipt Split Hold",
			"insert_after": "supplier_delivery_note",
			"read_only": 1,
			# Duplicates, amendments and returns start without a hold (receipt_hold.clear_hold_on_amend
			# also clears it on an amendment, which Frappe copies no_copy fields onto).
			"no_copy": 1,
			"print_hide": 1,
			"description": "The receipt split hold this receipt posts (IDEV-3334). Set by the stock lane "
			"only: the receipt's rows must be the hold's lines, and submitting it consumes the hold.",
		}
	],
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
