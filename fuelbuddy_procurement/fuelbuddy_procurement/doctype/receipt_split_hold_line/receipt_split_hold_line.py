# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""One line of a Receipt Split Hold (child table, field ``lines``): an advance or a purchase order
line, the purchase order line it books against, its unit and factor, and the quantity held, in that
unit and in litres. Never changed once the hold is placed."""

from frappe.model.document import Document


class ReceiptSplitHoldLine(Document):
	pass
