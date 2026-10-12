# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Purchase Advance Receipt Control (PARC).

One PARC per supplier advance: a draft created when a Payment Entry (Pay -> Supplier) is submitted
against a Purchase Order, for the quantity that advance pays for. An advance usually covers several
deliveries, so Purchase Receipts use it up bit by bit:

- A receipt row names the advance it books against (Purchase Receipt Item ``custom_parc``, created
  by ``install.py``) and books at most what the advance has available. Each use is a row in the
  PARC's ``consumptions`` table (Purchase Advance Consumption). What the advance has left is its
  quantity minus the quantity of its active rows.
- The PARC stays a draft (open) while quantity is left. The receipt that uses the last of it (within
  0.01) submits it (closed).
- Cancelling a receipt makes its rows inactive, so their quantity is back on the advance. A closed
  PARC is re-opened: Frappe cannot move a submitted document back to draft, so it is cancelled and
  a fresh draft copy is inserted with every row (the cancelled receipt's now inactive).
- Receipt split holds (IDEV-3334, fuelbuddy_procurement.allocation) reserve quantity on advances and
  purchase order lines for approved receipts until they post. A receipt at the ERP desk never takes
  held quantity: what an advance has available is what it has left minus what is held on it. A
  receipt may name any open advances, in any order, one row each (the oldest-first rule is retired).
  An advance whose Purchase Order is Closed or On Hold is skipped: ERPNext takes no receipt against
  it, so it cannot be named until the order is re-opened (``skipped_in_queue``).
- Cancelling the Payment Entry deletes its draft PARCs, or is refused while a receipt uses one or a
  live receipt split hold holds quantity on one.

The lane's receipt, which names a hold, is checked and booked by fuelbuddy_procurement.receipt_hold;
fuelbuddy_procurement.receipt_events sends each receipt one way or the other.

``get_open_advances`` and ``get_open_purchase_orders`` list a supplier's open advances and purchase
order lines with what each has available net of holds. ``hooks.py`` ``doc_events`` wires the
handlers onto Payment Entry and Purchase Receipt.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from fuelbuddy_procurement import allocation
from fuelbuddy_procurement.allocation import (
	CONSUMPTION,
	CONSUMPTIONS,
	EXPECTED,
	PARC,
	PO_STATUSES_TAKING_NO_RECEIPT,
	QTY_EPSILON,
)

# Purchase Receipt Item field: the advance this row books against.
PARC_FIELD = "custom_parc"


class PurchaseAdvanceReceiptControl(Document):
	def validate(self):
		# Stored for the form and the list view; every check recomputes both from the rows.
		self.qty_consumed = consumed_qty(self)
		self.qty_remaining = remaining_qty(self)


class ParcRefusedError(frappe.ValidationError):
	"""A Purchase Receipt names an advance it cannot use, or would take quantity it may not; the
	receipt is neither saved nor submitted."""


def consumed_qty(parc):
	"""Quantity the advance's active consumption rows book."""
	return sum(flt(row.qty) for row in parc.get(CONSUMPTIONS) or [] if row.is_active)


def remaining_qty(parc):
	"""What the advance has left: its quantity minus its active consumption."""
	return flt(parc.get(EXPECTED)) - consumed_qty(parc)


def _received_qty(purchase_order):
	"""Qty received to date against the PO across all submitted Purchase Receipts."""
	return flt(
		frappe.db.get_value(
			"Purchase Receipt Item", {"purchase_order": purchase_order, "docstatus": 1}, "sum(qty)"
		)
	)


def _holders_text(holders):
	return ", ".join(f"{holder.request_id} v{holder.version}" for holder in holders)


# ---- Payment Entry: After Submit -> one draft PARC per Purchase Order reference ----------
def create_parc_on_payment_entry(doc, method=None):
	if doc.payment_type != "Pay" or doc.party_type != "Supplier":
		return
	for ref in doc.references:
		if ref.reference_doctype != "Purchase Order" or flt(ref.allocated_amount) <= 0:
			continue
		po = frappe.get_doc("Purchase Order", ref.reference_name)
		if not po.items:
			continue
		# ponytail: fuel POs carry one line; the original script read items[0] too. Loop the
		# lines (and apportion the advance) if multi-line POs ever need PARC.
		item = po.items[0]
		advance_pct = flt(ref.allocated_amount) / flt(po.grand_total) if flt(po.grand_total) > 0 else 0
		qty_adv = flt(item.qty) * advance_pct
		frappe.get_doc(
			{
				"doctype": PARC,
				"payment_entry": doc.name,
				"purchase_order": po.name,
				"qty_of_po": item.qty,
				"uom_of_item": item.uom,
				"rate_of_po": item.rate,
				"grand_total_of_po": po.grand_total,
				"advance_against_po__summary_": po.advance_paid,
				"advance_paid": ref.allocated_amount,
				EXPECTED: qty_adv,
				"qty_left_to_be_received_from_po": flt(item.qty) - qty_adv - _received_qty(po.name),
			}
		).insert(ignore_permissions=True)


# ---- Payment Entry: On Cancel -> drop the draft PARCs this advance opened ------------------
def delete_draft_parcs_on_payment_entry_cancel(doc, method=None):
	"""Deletes the payment's draft PARCs, or refuses the cancel while a receipt uses one of them or a
	live receipt split hold holds quantity on one.

	A PARC that receipts closed is not a draft: it links the payment, so Frappe's own check refuses
	the cancel. A draft that receipts have used part of, or that a draft receipt names, is refused
	here, and so is one a hold names: the hold ends when its receipt posts or its request is
	cancelled or rejected. Receipts that used it and were cancelled since do not count, so the delete
	is forced past Frappe's link check, which would count them. The payment's advances are locked by
	name, which is their place in the oldest-first lock order (one payment), before the holds are
	read."""
	names = frappe.get_all(
		PARC, filters={"payment_entry": doc.name, "docstatus": 0}, pluck="name", order_by="name asc"
	)
	for name in names:
		parc = frappe.get_doc(PARC, name, for_update=True)
		receipts = _receipts_using(parc)
		if receipts:
			frappe.throw(
				_(
					"Advance {0} is in use by Purchase Receipt {1}. Cancel those receipts, or clear the "
					"advance on draft ones, before cancelling this payment."
				).format(name, ", ".join(receipts)),
				frappe.LinkExistsError,
			)
	holders = allocation.holders_by_parc(names, for_update=True)
	for name in names:
		if holders.get(name):
			frappe.throw(
				_(
					"Advance {0} is held for receipt split hold {1} (request {2}). A hold ends when its "
					"receipt posts or its request is cancelled or rejected; cancel this payment after that."
				).format(
					name,
					", ".join(holder.hold for holder in holders[name]),
					_holders_text(holders[name]),
				),
				frappe.LinkExistsError,
			)
	for name in names:
		frappe.delete_doc(PARC, name, ignore_permissions=True, force=True)


def _receipts_using(parc):
	"""Receipts that use the advance: its active consumption rows, and draft or submitted receipts
	whose rows name it."""
	active = {row.purchase_receipt for row in parc.get(CONSUMPTIONS) or [] if row.is_active}
	naming = frappe.get_all(
		"Purchase Receipt Item", filters={PARC_FIELD: parc.name, "docstatus": ["<", 2]}, pluck="parent"
	)
	return sorted(active | set(naming))


# ---- Purchase Receipt at the desk: which advances a row may name -----------------------------
def skipped_in_queue(adv):
	"""True while the advance's Purchase Order is Closed or On Hold (`adv`: anything with a
	``po_status``): ERPNext takes no receipt against the order, so no receipt and no split may name
	the advance until the order is re-opened. Read from the order's status at every check; nothing
	about it is stored on the advance."""
	return adv.get("po_status") in PO_STATUSES_TAKING_NO_RECEIPT


def advance_refusal(parc, row, receipt_supplier, advance_supplier, holders=None):
	"""Why receipt `row` cannot book against advance `parc`, or None when it can.

	The advance is open and is an advance to the receipt's supplier; the row is on its Purchase Order,
	in the unit it is counted in, and books some quantity but no more than the advance has available
	(within 0.01): what it has left, less what live receipt split holds (`holders`) hold on it. The
	rest of the receipt goes on rows without an advance."""
	if parc.docstatus == 1:
		if parc.purchase_receipt:
			return _("is used up: Purchase Receipt {0} used the last of it").format(parc.purchase_receipt)
		return _("is closed")
	if parc.docstatus == 2:
		return _("is cancelled")
	if advance_supplier != receipt_supplier:
		return _("is an advance to supplier {0}, not to {1}").format(advance_supplier, receipt_supplier)
	if row.purchase_order != parc.purchase_order:
		return _("is for Purchase Order {0}; this row is on {1}").format(
			parc.purchase_order, row.purchase_order or _("no Purchase Order")
		)
	if row.uom != parc.uom_of_item:
		return _("is counted in {0}; this row is in {1}").format(parc.uom_of_item, row.uom)
	booked, left = flt(row.qty), remaining_qty(parc)
	held = sum(flt(holder.qty) for holder in holders or [])
	if booked <= 0:
		return _("cannot be named on a row that books no quantity")
	if booked - (left - held) > QTY_EPSILON:
		if held > QTY_EPSILON:
			reason = _(
				"has {0:.3f} {1} left, {2:.3f} of it held for receipt split holds ({3}); this row books "
				"{4:.3f}. Book the rest on a row without an advance"
			).format(left, parc.uom_of_item, held, _holders_text(holders), booked)
		else:
			reason = _(
				"has {0:.3f} {1} left; this row books {2:.3f}. Book the rest on a row without an advance"
			).format(left, parc.uom_of_item, booked)
		last = _last_receipt(parc)
		return reason + (_(" (last used by Purchase Receipt {0})").format(last) if last else "")
	return None


def _last_receipt(parc):
	"""The receipt on the advance's latest active consumption row, if any."""
	rows = [row for row in parc.get(CONSUMPTIONS) or [] if row.is_active]
	return rows[-1].purchase_receipt if rows else None


def _order_refusal(order, company):
	"""Why no row may name an advance on Purchase Order `order` (supplier, company, status) on a
	receipt for `company`, or None."""
	if skipped_in_queue(frappe._dict(po_status=order.get("status"))):
		return _(
			"is skipped while its Purchase Order {0} is {1}: no receipt can use it until the order is "
			"re-opened"
		).format(order.get("name"), order.get("status"))
	if company and order.get("company") and order.get("company") != company:
		return _("is an advance of company {0}; this receipt is for {1}").format(order.get("company"), company)
	return None


def _named_advances(doc, for_update=False):
	"""[(PARC, row)] for the advances a desk receipt's rows name, or ParcRefusedError listing every row
	at fault, one line each. A return cannot name an advance.

	Each named advance must pass ``advance_refusal`` (net of live holds) and ``_order_refusal``, and
	be named on one row. The receipt may name any open advances of its supplier, in any order.

	With `for_update` (on submit) the named advances are re-read under a row lock held until the
	transaction ends, oldest first (the lock order of fuelbuddy_procurement.allocation), and the
	holds on them are read with a locking read: a receipt or a hold that has just used or held part
	of an advance shows as such, and this receipt is refused. ERPNext's own posting runs before this
	and can still end in a deadlock or lock wait timeout; nothing is saved then."""
	rows = [row for row in doc.get("items") or [] if row.get(PARC_FIELD)]
	if not rows:
		return []
	if doc.get("is_return"):
		frappe.throw(
			_("A return cannot use an advance (PARC); clear Advance (PARC) on its rows."), ParcRefusedError
		)
	names = list(dict.fromkeys(row.get(PARC_FIELD) for row in rows))
	if for_update:
		names = allocation.queue_order(names)
	parcs = {name: _read(name, for_update) for name in names}
	holders = allocation.holders_by_parc(names, for_update=for_update)
	orders = {}
	named, errors, seen = [], [], set()
	for row in rows:
		name = row.get(PARC_FIELD)
		where = _("Row {0}: advance {1}").format(row.idx, name)
		parc = parcs.get(name)
		if name in seen:
			errors.append((row.idx, _("{0} is named on more than one row").format(where)))
		elif parc is None:
			errors.append((row.idx, _("{0} does not exist").format(where)))
		else:
			if parc.purchase_order not in orders:
				orders[parc.purchase_order] = (
					frappe.db.get_value(
						"Purchase Order", parc.purchase_order, ["name", "supplier", "company", "status"], as_dict=True
					)
					if parc.purchase_order
					else None
				) or frappe._dict()
			order = orders[parc.purchase_order]
			reason = advance_refusal(parc, row, doc.supplier, order.get("supplier"), holders.get(name)) or (
				_order_refusal(order, doc.get("company")) if parc.docstatus == 0 else None
			)
			if reason:
				errors.append((row.idx, f"{where} {reason}"))
			else:
				named.append((parc, row))
		seen.add(name)
	if errors:
		message = "<br>".join(text for _idx, text in sorted(errors))
		frappe.throw(message, ParcRefusedError, title=_("Advance (PARC) refused"))
	return named


def _read(name, for_update=False):
	"""The PARC, or None when it does not exist (any more)."""
	if not frappe.db.exists(PARC, name):
		return None
	try:
		return frappe.get_doc(PARC, name, for_update=for_update)
	except frappe.DoesNotExistError:  # deleted by a transaction that committed after this one began
		frappe.clear_last_message()
		return None


# ---- Purchase Receipt at the desk: Validate -> refuse on save what the submit would refuse ----
def check_named_parcs_on_purchase_receipt(doc, method=None):
	_named_advances(doc)


# ---- Purchase Receipt at the desk: On Submit -> book the named advances, close used-up ones ---
def consume_named_parcs_on_purchase_receipt(doc, method=None):
	# Runs after ERPNext's own Purchase Receipt on_submit (Frappe calls the controller first, then
	# doc_events). The checks are repeated on locked re-reads: of two receipts racing for what an
	# advance has available, the later one is refused, even when both passed the save check.
	for parc, row in _named_advances(doc, for_update=True):
		book_consumption(parc, doc, row)


def book_consumption(parc, doc, row, hold=None):
	"""Books receipt `doc`'s row `row` against advance `parc` (locked): a consumption row, tagged with
	the receipt split hold `hold` that reserved it, if any. Saves the advance, or closes it (submits)
	once it has nothing left (within 0.01)."""
	parc.append(
		CONSUMPTIONS,
		{
			"purchase_receipt": doc.name,
			"purchase_receipt_item": row.name,
			"posting_date": doc.posting_date,
			"qty": flt(row.qty),
			"is_active": 1,
			"receipt_split_hold": hold,
		},
	)
	parc.flags.ignore_permissions = True
	left = remaining_qty(parc)
	if left > QTY_EPSILON:
		parc.save()
		frappe.msgprint(
			_("Advance {0}: {1:.3f} {2} booked, {3:.3f} left").format(
				parc.name, flt(row.qty), parc.uom_of_item, left
			),
			alert=True,
			indicator="green",
		)
		return
	parc.purchase_receipt = doc.name
	parc.qty_of_pr = consumed_qty(parc)
	parc.grand_total_of_pr = doc.grand_total
	# Received-to-date already includes this receipt (docstatus is 1 by on_submit).
	parc.qty_left_to_be_received_from_po = flt(parc.qty_of_po) - _received_qty(parc.purchase_order)
	parc.submit()
	frappe.msgprint(
		_("Advance {0} is used up; this receipt closed it").format(parc.name),
		alert=True,
		indicator="green",
	)


# ---- Purchase Receipt: On Cancel -> the quantity goes back on the advances it used ---------
def give_back_parcs_on_purchase_receipt_cancel(doc, method=None):
	"""Makes the receipt's consumption rows inactive, so their quantity is back on the advance, and
	re-opens an advance that was closed (``_reopen``). That includes an advance this receipt closed
	whole before consumption rows existed (purchase_receipt set, no rows): it re-opens in full. A
	receipt split hold the receipt posted stays Consumed.

	Runs before Frappe's back-link check, so cancelling the PARC that names this receipt in
	purchase_receipt is what lets the receipt cancel at all."""
	using = frappe.get_all(
		CONSUMPTION,
		filters={"parenttype": PARC, "purchase_receipt": doc.name, "is_active": 1, "docstatus": ["<", 2]},
		pluck="parent",
	)
	closed = frappe.get_all(PARC, filters={"purchase_receipt": doc.name, "docstatus": 1}, pluck="name")
	for name in sorted(set(using) | set(closed)):
		parc = frappe.get_doc(PARC, name, for_update=True)
		parc.flags.ignore_permissions = True
		if parc.docstatus == 0:
			_deactivate(parc, doc.name)
			parc.save()
		elif parc.docstatus == 1:
			_reopen(parc, doc.name)


def _deactivate(parc, receipt):
	for row in parc.get(CONSUMPTIONS) or []:
		if row.purchase_receipt == receipt:
			row.is_active = 0


def _reopen(parc, receipt):
	"""Cancels closed PARC `parc` and inserts a fresh draft copy with `receipt`'s rows inactive, since
	Frappe cannot move a submitted document back to draft. The copy keeps the payment and Purchase
	Order, and so its place in the oldest-first order, except among the advances of one payment, which
	go by name. The cancelled PARC keeps its rows as they were when it closed."""
	# Other receipts that used the advance still name it; they must not block its cancel.
	parc.ignore_linked_doctypes = ["Purchase Receipt"]
	parc.cancel()
	draft = frappe.copy_doc(parc)
	draft.docstatus = 0  # under tests copy_doc keeps the source's (now cancelled) docstatus
	draft.purchase_receipt = draft.qty_of_pr = draft.grand_total_of_pr = None
	_deactivate(draft, receipt)
	draft.qty_left_to_be_received_from_po = (
		flt(parc.qty_of_po) - flt(parc.get(EXPECTED)) - _received_qty(parc.purchase_order)
	)
	draft.insert(ignore_permissions=True)
	return draft


# ---- Lookups for the receipt approval screen (read-only) -----------------------------------
@frappe.whitelist(methods=["GET"])
def get_open_advances(
	supplier: str, company: str | None = None, item_code: str | None = None, for_request: str | None = None
):
	"""A supplier's open advances with quantity left, oldest first (payment date, then the order the
	payments were entered, then name): name, purchase_order, purchase_order_item, payment_entry,
	payment_date, advance_paid, the advance's quantity, qty_consumed, qty_remaining, uom_of_item,
	conversion_factor, company, po_status, skipped, stuck, qty_held, qty_available,
	qty_available_litres, held_by, setup_key, unit_problem (fuelbuddy_procurement.allocation
	advance_row). An advance whose Purchase Order is Closed or On Hold is listed in its place with
	skipped true; one whose order line has nothing left to receive with stuck true. qty_available is
	what a receipt or a split may take: net of live receipt split holds, leaving out `for_request`'s
	own. With `item_code`, only advances on orders with a line for that item. Read-only."""
	frappe.has_permission(PARC, "read", throw=True)
	view = allocation.snapshot(supplier, company, item_code, for_request=for_request)
	return [allocation.advance_row(view, adv) for adv in allocation.open_advances(view)]


@frappe.whitelist(methods=["GET"])
def get_open_purchase_orders(
	supplier: str, company: str | None = None, item_code: str | None = None, for_request: str | None = None
):
	"""The supplier's purchase order lines with quantity still to receive, oldest order first (order
	date, then the order they were entered, then name and line), leaving out orders that are Closed or
	On Hold, which ERPNext refuses a receipt against: purchase_order, transaction_date, schedule_date,
	company, purchase_order_item, item_code, uom, conversion_factor, qty, received_qty,
	qty_to_receive, stuck (always false), qty_held, qty_available, qty_available_litres, held_by,
	setup_key, unit_problem (fuelbuddy_procurement.allocation line_row). qty_held counts advance and
	order lines of live receipt split holds alike, leaving out `for_request`'s own. With `item_code`,
	only lines for that item. Read-only."""
	frappe.has_permission("Purchase Order", "read", throw=True)
	view = allocation.snapshot(supplier, company, item_code, for_request=for_request)
	return [allocation.line_row(view, line) for line in allocation.open_lines(view)]
