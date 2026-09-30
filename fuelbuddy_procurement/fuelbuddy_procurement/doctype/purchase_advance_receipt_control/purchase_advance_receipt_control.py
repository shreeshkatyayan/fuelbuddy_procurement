# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Purchase Advance Receipt Control (PARC).

One PARC per supplier advance: a draft created when a Payment Entry (Pay -> Supplier) is submitted
against a Purchase Order, for the quantity that advance pays for. An advance usually covers several
deliveries, so Purchase Receipts use it up bit by bit:

- A receipt row names the advance it books against (Purchase Receipt Item ``custom_parc``, created
  by ``install.py``) and books at most what the advance has left. Each use is a row in the PARC's
  ``consumptions`` table (Purchase Advance Consumption). What the advance has left is its quantity
  minus the quantity of its active rows.
- The PARC stays a draft (open) while quantity is left. The receipt that uses the last of it (within
  0.01) submits it (closed).
- Cancelling a receipt makes its rows inactive, so their quantity is back on the advance. A closed
  PARC is re-opened: Frappe cannot move a submitted document back to draft, so it is cancelled and
  a fresh draft copy is inserted with every row (the cancelled receipt's now inactive).
- Oldest advance first is enforced, on save and again on submit: a receipt may name only its
  supplier's oldest advance with quantity left, and only one (``advances_a_receipt_may_name``). The
  rest of the receipt goes on rows without an advance.
- Cancelling the Payment Entry deletes its draft PARCs, or is refused while a receipt uses one.

``get_open_advances`` lists a supplier's open advances oldest first with what each has left, and
``get_open_purchase_orders`` suggests purchase orders for the rest of a receipt, oldest first.
``hooks.py`` ``doc_events`` wires the handlers onto Payment Entry and Purchase Receipt.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

PARC = "Purchase Advance Receipt Control"
# Child table of PARC (field CONSUMPTIONS): one row per receipt that booked quantity against it.
CONSUMPTION = "Purchase Advance Consumption"
CONSUMPTIONS = "consumptions"
EXPECTED = "qty_to_be_received_against_the_advance_paid"
# Purchase Receipt Item field: the advance this row books against.
PARC_FIELD = "custom_parc"
# Quantities closer than this are treated as equal (float dust).
QTY_EPSILON = 0.01
# ERPNext refuses a Purchase Receipt against a Purchase Order in these states.
PO_STATUSES_TAKING_NO_RECEIPT = ("Closed", "On Hold")


class PurchaseAdvanceReceiptControl(Document):
	def validate(self):
		# Stored for the form and the list view; every check recomputes both from the rows.
		self.qty_consumed = consumed_qty(self)
		self.qty_remaining = remaining_qty(self)


class ParcRefusedError(frappe.ValidationError):
	"""A Purchase Receipt names an advance it cannot use; the receipt is neither saved nor submitted."""


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
	"""Deletes the payment's draft PARCs, or refuses the cancel while a receipt uses one of them.

	A PARC that receipts closed is not a draft: it links the payment, so Frappe's own check refuses
	the cancel. A draft that receipts have used part of, or that a draft receipt names, is refused
	here. Receipts that used it and were cancelled since do not count, so the delete is forced past
	Frappe's link check, which would count them."""
	for name in frappe.get_all(PARC, filters={"payment_entry": doc.name, "docstatus": 0}, pluck="name"):
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
		frappe.delete_doc(PARC, name, ignore_permissions=True, force=True)


def _receipts_using(parc):
	"""Receipts that use the advance: its active consumption rows, and draft or submitted receipts
	whose rows name it."""
	active = {row.purchase_receipt for row in parc.get(CONSUMPTIONS) or [] if row.is_active}
	naming = frappe.get_all(
		"Purchase Receipt Item", filters={PARC_FIELD: parc.name, "docstatus": ["<", 2]}, pluck="parent"
	)
	return sorted(active | set(naming))


# ---- Purchase Receipt: which advances a receipt may name -----------------------------------
def advances_a_receipt_may_name(open_advances):
	"""The advances one receipt may name, out of `open_advances` (the supplier's advances with quantity
	left, oldest first).

	Working rule, kept in this one function so that it can be switched: the oldest advance, and only
	that one. The rest of the receipt goes on rows without an advance and is mapped to purchase orders
	by hand; it does not spill onto the next advance. Returning more advances here lets a receipt name
	up to that many, one row each."""
	return open_advances[:1]


def advance_refusal(parc, row, receipt_supplier, advance_supplier):
	"""Why receipt `row` cannot book against advance `parc`, or None when it can.

	The advance is open and is an advance to the receipt's supplier; the row is on its Purchase Order,
	in the unit it is counted in, and books some quantity but no more than the advance has left
	(within 0.01). The rest of the receipt goes on rows without an advance."""
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
	if booked <= 0:
		return _("cannot be named on a row that books no quantity")
	if booked - left > QTY_EPSILON:
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


def _order_refusal(name, accepted, allowed, supplier):
	"""Why the receipt may not name advance `name` under ``advances_a_receipt_may_name``, or None.

	`accepted`: [(PARC, row)] this receipt already uses on earlier rows; `allowed`: what the rule
	lets it name, oldest first."""
	if name in [adv.name for adv in allowed]:
		return None
	if not allowed:
		return _("is not an open advance of {0} for this receipt's company").format(supplier)
	if len(accepted) >= len(allowed):
		first, first_row = accepted[0]
		return _(
			"cannot be used as well: a receipt uses at most {0} advance(s), and row {1} names {2}. Book "
			"this quantity on a row without an advance"
		).format(len(allowed), first_row.idx, first.name)
	oldest = allowed[len(accepted)]
	reason = _(
		"is not the oldest advance of {0} with quantity left: use {1} first ({2:.3f} {3} left on "
		"Purchase Order {4}, paid {5})"
	).format(
		supplier,
		oldest.name,
		oldest.qty_remaining,
		oldest.uom_of_item,
		oldest.purchase_order,
		oldest.payment_date,
	)
	if oldest.get("po_status") in PO_STATUSES_TAKING_NO_RECEIPT:
		# Still first in line: the rule does not skip it. Say why no receipt can use it as things stand.
		reason += _(
			". Purchase Order {0} is {1}, so no receipt can be booked against it as it stands"
		).format(oldest.purchase_order, oldest.po_status)
	return reason


def _named_advances(doc, for_update=False):
	"""[(PARC, row)] for the advance the receipt's rows name, or ParcRefusedError listing every row at
	fault, one line each. A return cannot name an advance.

	Each named advance must pass ``advance_refusal`` and ``advances_a_receipt_may_name``: today one
	advance, named on one row, the supplier's oldest with quantity left within the receipt's company.

	With `for_update` (on submit) every PARC the decision reads is re-read under a row lock held until
	the transaction ends: the supplier's open advances queued ahead of the named one, then the named
	one, oldest first so that receipts racing for the same advances lock them in one order. A locking
	read sees the latest committed rows, so an advance that another receipt has just used up, or used
	part of, shows as such and this receipt is refused. What it cannot see: an advance re-opened
	(re-inserted) or paid by a transaction that commits while this one runs. ERPNext's own posting
	runs before this and can still end in a deadlock or lock wait timeout; nothing is saved then."""
	rows = [row for row in doc.get("items") or [] if row.get(PARC_FIELD)]
	if not rows:
		return []
	if doc.get("is_return"):
		frappe.throw(
			_("A return cannot use an advance (PARC); clear Advance (PARC) on its rows."), ParcRefusedError
		)
	queue = _supplier_advances(doc.supplier, doc.company)
	parcs = _read_advances(list(dict.fromkeys(row.get(PARC_FIELD) for row in rows)), queue, for_update)
	allowed = advances_a_receipt_may_name(_with_quantity_left(queue, parcs))
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
			supplier = parc.purchase_order and frappe.db.get_value(
				"Purchase Order", parc.purchase_order, "supplier"
			)
			reason = advance_refusal(parc, row, doc.supplier, supplier) or _order_refusal(
				name, named, allowed, doc.supplier
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


def _read_advances(names, queue, for_update):
	"""{name: PARC or None} for the named advances and, on submit, for the supplier's open advances
	queued ahead of them. On submit every read is a locking read, in one order: the queued advances
	oldest first up to the newest named one, then any named advance not in the queue, by name."""
	if for_update:
		order = [adv.name for adv in queue]
		ahead = max((order.index(name) for name in names if name in order), default=-1)
		names = order[: ahead + 1] + sorted(name for name in names if name not in order)
	return {name: _read(name, for_update) for name in names}


def _read(name, for_update=False):
	"""The PARC, or None when it does not exist (any more)."""
	if not frappe.db.exists(PARC, name):
		return None
	try:
		return frappe.get_doc(PARC, name, for_update=for_update)
	except frappe.DoesNotExistError:  # deleted by a transaction that committed after this one began
		frappe.clear_last_message()
		return None


def _with_quantity_left(queue, parcs):
	"""The queue (oldest first) cut to the advances with quantity left, taking each advance's state
	from `parcs` where it was read (under lock on submit), from the queue's snapshot otherwise."""
	left = []
	for adv in queue:
		if adv.name in parcs:
			parc = parcs[adv.name]
			if parc is None or parc.docstatus != 0:
				continue
			adv = frappe._dict(adv, qty_remaining=remaining_qty(parc))
		if adv.qty_remaining > QTY_EPSILON:
			left.append(adv)
	return left


def _supplier_advances(supplier, company=None):
	"""The supplier's open (draft) advances, oldest first, each with qty_consumed and qty_remaining,
	as this transaction's snapshot shows them.

	Oldest first: the Payment Entry's posting date, then the order the payments were entered
	(creation), then PARC name. The name only orders the advances one payment opened (on several
	Purchase Orders, or on several payment terms of one): it is creation order under the site's PARC
	naming rule, a random hash without one."""
	advances = _draft_advances(supplier, company)
	used = _consumed_by([adv.name for adv in advances])
	for adv in advances:
		adv.qty_consumed = used.get(adv.name, 0.0)
		adv.qty_remaining = flt(adv.get(EXPECTED)) - adv.qty_consumed
	return sorted(advances, key=_oldest_first)


def _oldest_first(adv):
	return (adv.payment_date, adv.payment_created, adv.name)


def _draft_advances(supplier, company=None):
	"""The supplier's draft PARCs on submitted payments, with the payment's posting date and creation."""
	parc = frappe.qb.DocType(PARC)
	po = frappe.qb.DocType("Purchase Order")
	pe = frappe.qb.DocType("Payment Entry")
	query = (
		frappe.qb.from_(parc)
		.join(po)
		.on(po.name == parc.purchase_order)
		.join(pe)
		.on(pe.name == parc.payment_entry)
		.select(
			parc.name,
			parc.purchase_order,
			parc.payment_entry,
			pe.posting_date.as_("payment_date"),
			pe.creation.as_("payment_created"),
			parc.advance_paid,
			parc[EXPECTED],
			parc.uom_of_item,
			po.company,
			po.status.as_("po_status"),
		)
		.where((parc.docstatus == 0) & (pe.docstatus == 1) & (po.supplier == supplier))
	)
	if company:
		query = query.where(po.company == company)
	return query.run(as_dict=True)


def _consumed_by(names):
	"""{PARC name: quantity of its active consumption rows} for the named PARCs."""
	used = {}
	if names:
		for row in frappe.get_all(
			CONSUMPTION,
			filters={"parenttype": PARC, "parent": ["in", names], "is_active": 1},
			fields=["parent", "qty"],
		):
			used[row.parent] = used.get(row.parent, 0.0) + flt(row.qty)
	return used


# ---- Purchase Receipt: Validate -> refuse on save what the submit would refuse -------------
def check_named_parcs_on_purchase_receipt(doc, method=None):
	_named_advances(doc)


# ---- Purchase Receipt: On Submit -> book the named advance, close it once used up ---------
def consume_named_parcs_on_purchase_receipt(doc, method=None):
	# Runs after ERPNext's own Purchase Receipt on_submit (Frappe calls the controller first, then
	# doc_events). The checks are repeated on locked re-reads: of two receipts racing for what an
	# advance has left, the later one is refused, even when both passed the save check.
	for parc, row in _named_advances(doc, for_update=True):
		parc.append(
			CONSUMPTIONS,
			{
				"purchase_receipt": doc.name,
				"purchase_receipt_item": row.name,
				"posting_date": doc.posting_date,
				"qty": flt(row.qty),
				"is_active": 1,
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
			continue
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
	whole before consumption rows existed (purchase_receipt set, no rows): it re-opens in full.

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
def get_open_advances(supplier: str, company: str | None = None):
	"""A supplier's open advances with quantity left, oldest first (see ``_supplier_advances``): name,
	purchase_order, payment_entry, payment_date, advance_paid, the advance's quantity, qty_consumed,
	qty_remaining, uom_of_item, company, po_status. The first is the only one the next receipt may name,
	for at most its qty_remaining. An advance whose Purchase Order is Closed or On Hold stays in line
	(the rule does not skip it); po_status shows it. Read-only."""
	frappe.has_permission(PARC, "read", throw=True)
	return [
		frappe._dict({key: value for key, value in adv.items() if key != "payment_created"})
		for adv in _supplier_advances(supplier, company)
		if adv.qty_remaining > QTY_EPSILON
	]


@frappe.whitelist(methods=["GET"])
def get_open_purchase_orders(supplier: str, company: str | None = None):
	"""A suggestion for the part of a receipt beyond its advance: the supplier's purchase order lines
	with quantity still to receive, oldest order first (order date, then the order they were entered,
	then name and line). Leaves out orders that are closed or on hold, which ERPNext refuses a receipt
	against. Nothing checks a receipt against this list; the approver chooses. Read-only."""
	frappe.has_permission("Purchase Order", "read", throw=True)
	po = frappe.qb.DocType("Purchase Order")
	item = frappe.qb.DocType("Purchase Order Item")
	to_receive = item.qty - item.received_qty
	query = (
		frappe.qb.from_(po)
		.join(item)
		.on((item.parent == po.name) & (item.parenttype == "Purchase Order"))
		.select(
			po.name.as_("purchase_order"),
			po.transaction_date,
			po.schedule_date,
			po.company,
			item.name.as_("purchase_order_item"),
			item.item_code,
			item.uom,
			item.qty,
			item.received_qty,
			to_receive.as_("qty_to_receive"),
		)
		.where(
			(po.docstatus == 1)
			& (po.supplier == supplier)
			& po.status.notin(["Closed", "On Hold"])
			& (to_receive > QTY_EPSILON)
		)
		.orderby(po.transaction_date)
		.orderby(po.creation)
		.orderby(po.name)
		.orderby(item.idx)
	)
	if company:
		query = query.where(po.company == company)
	return query.run(as_dict=True)
