# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""One row per Purchase Receipt that booked quantity against an advance (child table of Purchase
Advance Receipt Control, field ``consumptions``). Rows are never deleted: a cancelled receipt's row
is made inactive."""

from frappe.model.document import Document


class PurchaseAdvanceConsumption(Document):
	pass
