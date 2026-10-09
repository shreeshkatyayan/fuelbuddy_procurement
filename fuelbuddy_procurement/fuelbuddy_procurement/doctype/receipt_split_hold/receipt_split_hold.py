# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Receipt Split Hold (IDEV-3334): one version of an approved fuel receipt's split, named
``{request_id}-v{version}``. Written only by fuelbuddy_procurement.api.receipt_split_hold (place,
release) and by the posting of the Purchase Receipt that names it (fuelbuddy_procurement.receipt_hold),
each under row locks; nobody creates, edits or deletes one at the desk. Changes are tracked."""

import frappe
from frappe import _
from frappe.model.document import Document


def hold_name(request_id, version):
	return f"{request_id}-v{int(version)}"


class ReceiptSplitHold(Document):
	def autoname(self):
		self.name = hold_name(self.request_id, self.version)

	def on_trash(self):
		frappe.throw(
			_("A Receipt Split Hold is never deleted: a released, rejected or consumed version stays as the record of that attempt."),
			frappe.ValidationError,
		)
