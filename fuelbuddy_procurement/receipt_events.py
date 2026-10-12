# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Purchase Receipt events (IDEV-3334): each receipt goes one of two ways.

- Hold path: a receipt that names a receipt split hold (``custom_receipt_split_hold``, the stock
  lane's GRN) is checked and booked by fuelbuddy_procurement.receipt_hold.
- Desk path: any other receipt. Its advances are checked and booked by the PARC handlers
  (``check_named_parcs_on_purchase_receipt``, ``consume_named_parcs_on_purchase_receipt``: never
  more than an advance has available net of holds), and its purchase order lines here:

    always             a line that receipt split holds hold quantity on may receive no more than
                       what it has left less what is held, so a desk receipt never takes held
                       quantity (with no holds this checks nothing);
    with the site flag a hard cap on every line: no more than what it has left less what is held,
    (DESK_RULES_FLAG)  so the over-receipt allowance no longer applies to purchase receipts.

  The save checks a snapshot, for an early message. The submit decides: it runs after ERPNext's own
  posting (which has added this receipt to the lines' received_qty, under its row locks), locks the
  named advances, then reads the holds with a locking read (fuelbuddy_procurement.allocation, steps
  1, 3 and 5). Refusals are ParcRefusedError, one line per row at fault.

DESK_RULES_FLAG is read from site_config.json: ``bench --site <site> set-config -p
fuelbuddy_procurement_desk_rules 1`` switches the cap on, 0 (or removing it) off.
"""

import frappe
from frappe import _
from frappe.utils import flt

from fuelbuddy_procurement import allocation, receipt_hold
from fuelbuddy_procurement.allocation import QTY_EPSILON
from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control import (
	purchase_advance_receipt_control as parc_handlers,
)

DESK_RULES_FLAG = "fuelbuddy_procurement_desk_rules"


def validate(doc, method=None):
	if doc.get(receipt_hold.HOLD_FIELD):
		receipt_hold.check_on_save(doc)
		return
	parc_handlers.check_named_parcs_on_purchase_receipt(doc)
	_refuse(po_line_problems(doc))


def on_submit(doc, method=None):
	if doc.get(receipt_hold.HOLD_FIELD):
		receipt_hold.consume(doc)
		return
	# Advances first (lock step 3), then the holds on the order lines (step 5). A refusal here rolls
	# back the bookings with the rest of the receipt.
	parc_handlers.consume_named_parcs_on_purchase_receipt(doc)
	_refuse(po_line_problems(doc, submitted=True))


def desk_rules_on():
	"""True while the site flag switches the desk receipt's hard purchase order cap on."""
	return bool(frappe.utils.cint(frappe.conf.get(DESK_RULES_FLAG)))


def received_on_lines(doc):
	"""{purchase order line: (quantity this receipt receives on it, [row idx])}. ERPNext adds the
	rows' received_qty to the line's received_qty."""
	received = {}
	for row in doc.get("items") or []:
		line = row.get("purchase_order_item")
		if not line:
			continue
		qty, rows = received.get(line, (0.0, []))
		received[line] = (qty + (flt(row.get("received_qty")) or flt(row.get("qty"))), [*rows, row.get("idx")])
	return received


def po_line_problems(doc, submitted=False):
	"""Row messages for the purchase order lines this desk receipt may not receive as much on: lines
	with holds always, every line with the site flag. `submitted`: called on submit, when ERPNext has
	already added this receipt to received_qty, and the reads lock."""
	if doc.get("is_return"):
		return []
	received = received_on_lines(doc)
	if not received:
		return []
	cap_every_line = desk_rules_on()
	lines = allocation.read_lines(list(received), for_update=submitted)
	holders = allocation.holders_by_line(list(received), for_update=submitted)
	problems = []
	for name in sorted(received):
		line = lines.get(name)
		if line is None:  # ERPNext refuses a row on a line that does not exist
			continue
		booked, rows = received[name]
		on_line = holders.get(name, [])
		held = sum(flt(holder.qty) for holder in on_line)
		if held <= QTY_EPSILON and not cap_every_line:
			continue
		left = flt(line.qty) - flt(line.received_qty) + (booked if submitted else 0.0)
		if booked - (left - held) <= QTY_EPSILON:
			continue
		where = _("Row {0}").format(", ".join(str(idx) for idx in rows))
		if held > QTY_EPSILON:
			problems.append(
				_(
					"{0}: Purchase Order {1} line {2} has {3:.3f} {4} left to receive, {5:.3f} of it held "
					"for receipt split holds ({6}); this receipt receives {7:.3f}. A desk receipt cannot "
					"take held quantity."
				).format(
					where,
					line.purchase_order,
					name,
					left,
					line.uom,
					held,
					", ".join(f"{holder.request_id} v{holder.version}" for holder in on_line),
					booked,
				)
			)
		else:
			problems.append(
				_(
					"{0}: Purchase Order {1} line {2} has {3:.3f} {4} left to receive; this receipt receives "
					"{5:.3f}. Receiving more than is left is switched off ({6})."
				).format(where, line.purchase_order, name, left, line.uom, booked, DESK_RULES_FLAG)
			)
	return problems


def _refuse(problems):
	if problems:
		frappe.throw(
			"<br>".join(problems), parc_handlers.ParcRefusedError, title=_("Purchase Order quantity refused")
		)
