# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""A Purchase Receipt that posts a receipt split hold (IDEV-3334): the hold path.

The stock lane's GRN names the hold it posts in ``custom_receipt_split_hold`` (install.py) and is
built from the hold's lines: one row per line, on the line's purchase order line, naming the line's
advance (``custom_parc``) on advance lines, for the line's quantity in the line's unit.

On every save (``check_on_save``) the receipt is refused, with ReceiptHoldRefusedError naming each
problem, unless the hold is Held, the receipt carries the hold's op key (``custom_app_op_key``),
supplier and company, and its rows are the hold's lines (purchase order line, advance, item, unit
and factor, and quantity within 0.01).

On submit (``consume``), after ERPNext's own posting and in the receipt's transaction, under the
locks of fuelbuddy_procurement.allocation (steps 1 to 5): the same checks on the hold as it is now,
then every line against what ERP has now, leaving this hold out of what is held (each advance still
open with the line's quantity left after other holds, each order line still able to take it after
other holds, the unit and factor unchanged). Then each advance line books its row against its
advance (tagged with the hold) and closes advances that are used up, and the hold becomes Consumed.
All or nothing, with the receipt.

A receipt cancelled later gives its quantity back to the advances as any receipt does; its hold
stays Consumed.
"""

import frappe
from frappe import _
from frappe.utils import flt

from fuelbuddy_procurement import allocation
from fuelbuddy_procurement.allocation import CONSUMED, HELD, HOLD, PARC_LINE, QTY_EPSILON, RELEASED
from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control import (
	PARC,
	PARC_FIELD,
	ParcRefusedError,
	book_consumption,
)

# Purchase Receipt field (install.py): the hold this receipt posts.
HOLD_FIELD = "custom_receipt_split_hold"
# Purchase Receipt field (fuelbuddy_crm add_app_op_key): the app operation behind the receipt.
OP_KEY_FIELD = "custom_app_op_key"
# A receipt row's conversion factor equals its hold line's within this.
FACTOR_TOLERANCE = 1e-6


class ReceiptHoldRefusedError(ParcRefusedError):
	"""A Purchase Receipt cannot post the receipt split hold it names; it is neither saved nor
	submitted. erp-functions answers it GRN_MAPPING_REFUSED."""


def clear_hold_on_amend(doc, method=None):
	"""before_insert: an amendment of a cancelled lane receipt starts without its hold, which is
	Consumed (Frappe copies no_copy fields onto an amendment)."""
	if doc.get("amended_from") and doc.get(HOLD_FIELD):
		doc.set(HOLD_FIELD, None)


def check_on_save(doc):
	_refuse(receipt_problems(doc, read_hold(doc.get(HOLD_FIELD))))


def consume(doc):
	"""on_submit: re-check the hold and its lines under locks, book the advances, mark it Consumed."""
	name = doc.get(HOLD_FIELD)
	hold = read_hold(name)  # the lines never change once the hold is placed
	if hold is None:
		_refuse(receipt_problems(doc, None))
	sources = allocation.read_sources(  # steps 1 to 3; ERPNext's posting already locked the order lines
		hold.supplier,
		hold.company,
		hold.item_code,
		parcs=[line.parc for line in hold.lines if line.source_type == PARC_LINE],
		purchase_orders=[line.purchase_order for line in hold.lines if line.purchase_order],
		discover=False,
		for_update=True,
	)
	current = frappe.get_doc(HOLD, name, for_update=True)  # step 4
	problems = receipt_problems(doc, hold, current)
	if not problems:
		view = allocation.build_view(
			sources,
			allocation.read_held(sources, exclude_hold=name, for_update=True),  # step 5
			received_now=received_by(doc),
		)
		units = {int(line.line_no): (line.uom, line.conversion_factor) for line in hold.lines}
		refusals, _resolved = allocation.check_lines(view, hold.lines, units=units)
		problems = [refusal["message"] for refusal in refusals]
	_refuse(problems, hold=name)

	rows = {row_key(row): row for row in doc.get("items") or []}
	for line in sorted(hold.lines, key=lambda line: int(line.line_no)):
		if line.source_type == PARC_LINE:
			parc = frappe.get_doc(PARC, line.parc, for_update=True)  # locked above: no wait
			book_consumption(parc, doc, rows[line_key(line)], hold=name)

	current.status = CONSUMED
	current.live_request_id = None
	current.purchase_receipt = doc.name
	current.consumed_at = frappe.utils.now_datetime()
	current.flags.ignore_links = True
	current.save(ignore_permissions=True)


def read_hold(name):
	"""The hold and its lines as _dicts, read without locks; None when there is no such hold."""
	if not name or not frappe.db.exists(HOLD, name):
		return None
	doc = frappe.get_doc(HOLD, name)
	return frappe._dict(
		name=doc.name,
		request_id=doc.request_id,
		version=doc.version,
		status=doc.status,
		supplier=doc.supplier,
		company=doc.company,
		item_code=doc.item_code,
		op_key=doc.op_key,
		purchase_receipt=doc.purchase_receipt,
		release_reason=doc.release_reason,
		lines=[
			frappe._dict(
				line_no=line.line_no,
				source_type=line.source_type,
				parc=line.parc or None,
				purchase_order=line.purchase_order,
				purchase_order_item=line.purchase_order_item,
				uom=line.uom,
				conversion_factor=line.conversion_factor,
				qty=line.qty,
				qty_litres=line.qty_litres,
			)
			for line in doc.get("lines") or []
		],
	)


def row_key(row):
	"""A receipt row's place in its hold: its purchase order line and the advance it names, if any."""
	return (row.get("purchase_order_item") or None, row.get(PARC_FIELD) or None)


def line_key(line):
	return (line.purchase_order_item or None, line.parc or None)


def received_by(doc):
	"""{purchase order line: what this receipt adds to its received_qty} (ERPNext sums the rows'
	received_qty)."""
	received = {}
	for row in doc.get("items") or []:
		line = row.get("purchase_order_item")
		if line:
			received[line] = received.get(line, 0.0) + (flt(row.get("received_qty")) or flt(row.get("qty")))
	return received


def receipt_problems(doc, hold, current=None):
	"""Why receipt `doc` cannot post `hold` (as read; `current`: the hold as locked now, for its
	status), one message each; [] when it can."""
	name = doc.get(HOLD_FIELD)
	if hold is None:
		return [_("Receipt split hold {0} does not exist").format(name)]
	state = current or hold
	problems = []
	if state.status != HELD:
		problems.append(_status_problem(name, state))
	if doc.get("is_return"):
		problems.append(_("A return cannot post a receipt split hold"))
	if (doc.get(OP_KEY_FIELD) or None) != (hold.op_key or None):
		problems.append(
			_("This receipt's op key is {0}; hold {1} may only be posted by {2}").format(
				doc.get(OP_KEY_FIELD) or _("empty"), name, hold.op_key
			)
		)
	if doc.get("supplier") != hold.supplier:
		problems.append(_("Hold {0} is for supplier {1}, not {2}").format(name, hold.supplier, doc.get("supplier")))
	if doc.get("company") != hold.company:
		problems.append(_("Hold {0} is for company {1}, not {2}").format(name, hold.company, doc.get("company")))
	return problems + row_problems(doc.get("items") or [], hold)


def _status_problem(name, hold):
	if hold.status == CONSUMED:
		return _("Hold {0} was already posted by Purchase Receipt {1}").format(name, hold.purchase_receipt)
	if hold.status == RELEASED:
		return _("Hold {0} was released ({1}); the receipt has to be approved again").format(
			name, hold.release_reason
		)
	return _("Hold {0} was rejected: it holds nothing").format(name)



def row_problems(rows, hold):
	"""Why the receipt's rows are not the hold's lines: each row must be one line (purchase order
	line and advance), with the line's order, the hold's item, the line's unit and factor, and its
	quantity within 0.01; each line must have its row."""
	lines = {line_key(line): line for line in hold.lines}
	matched, problems = set(), []
	for row in rows:
		key = row_key(row)
		at = _("Row {0}").format(row.get("idx"))
		line = lines.get(key)
		if line is None:
			problems.append(
				_("{0}: Purchase Order line {1}{2} is not a line of hold {3}").format(
					at,
					key[0] or _("(none)"),
					_(" with advance {0}").format(key[1]) if key[1] else "",
					hold.name,
				)
			)
			continue
		if key in matched:
			problems.append(_("{0}: hold line {1} has more than one row").format(at, line.line_no))
			continue
		matched.add(key)
		if row.get("purchase_order") != line.purchase_order:
			problems.append(
				_("{0}: is on Purchase Order {1}; hold line {2} is on {3}").format(
					at, row.get("purchase_order"), line.line_no, line.purchase_order
				)
			)
		if row.get("item_code") != hold.item_code:
			problems.append(_("{0}: item {1}; hold {2} is for {3}").format(at, row.get("item_code"), hold.name, hold.item_code))
		if row.get("uom") != line.uom or abs(flt(row.get("conversion_factor")) - flt(line.conversion_factor)) > FACTOR_TOLERANCE:
			problems.append(
				_("{0}: {1} at {2}; hold line {3} is {4} at {5}").format(
					at, row.get("uom"), flt(row.get("conversion_factor")), line.line_no, line.uom, flt(line.conversion_factor)
				)
			)
		if abs(flt(row.get("qty")) - flt(line.qty)) > QTY_EPSILON:
			problems.append(
				_("{0}: books {1:.3f} {2}; hold line {3} holds {4:.3f}").format(
					at, flt(row.get("qty")), row.get("uom"), line.line_no, flt(line.qty)
				)
			)
	for key, line in lines.items():
		if key not in matched:
			problems.append(
				_("Hold line {0} (Purchase Order line {1}{2}) has no row on this receipt").format(
					line.line_no, key[0], _(", advance {0}").format(key[1]) if key[1] else ""
				)
			)
	return problems


def _refuse(problems, hold=None):
	if problems:
		frappe.throw(
			"<br>".join(problems),
			ReceiptHoldRefusedError,
			title=_("Receipt split hold {0} refused").format(hold) if hold else _("Receipt split hold refused"),
		)
