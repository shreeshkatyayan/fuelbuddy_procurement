# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Purchase Advance Receipt Control (PARC).

One PARC row per supplier advance: created as a draft when a Payment Entry (Pay -> Supplier) is
submitted against a Purchase Order. A Purchase Receipt uses an advance only by naming it on the
row that books it (Purchase Receipt Item ``custom_parc``, created by ``install.py``). Submitting the
receipt closes (submits) exactly the advances it names, or refuses the receipt; nothing is matched
by quantity. Cancelling the receipt cancels the PARCs it closed and re-opens each advance as a
fresh draft. Cancelling the Payment Entry deletes its draft PARCs; a PARC closed by a receipt, or
named on a draft receipt, blocks that cancel through Frappe's normal link check.

``get_open_advances`` lists a supplier's open advances, oldest payment first, for the GRN approval
screen. That order is a suggestion: the receipt check does not enforce it, so a row may name any
open advance on its Purchase Order even while an older one is open. ``hooks.py`` ``doc_events``
wires the handlers onto Payment Entry / Purchase Receipt.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

PARC = "Purchase Advance Receipt Control"
EXPECTED = "qty_to_be_received_against_the_advance_paid"
# Purchase Receipt Item field: the advance this row uses.
PARC_FIELD = "custom_parc"
# Below this, the advance's qty and the row's qty are treated as equal (float dust).
QTY_EPSILON = 0.01


class PurchaseAdvanceReceiptControl(Document):
	pass


class ParcRefusedError(frappe.ValidationError):
	"""A Purchase Receipt names an advance it cannot use; the receipt is neither saved nor submitted."""


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
	for name in frappe.get_all(PARC, filters={"payment_entry": doc.name, "docstatus": 0}, pluck="name"):
		frappe.delete_doc(PARC, name, ignore_permissions=True)


# ---- Purchase Receipt: which named advances a receipt may use ------------------------------
def advance_refusal(parc, row, receipt_supplier, advance_supplier):
	"""Why receipt `row` cannot use advance `parc`, or None when it can.

	An advance is used whole, by one row: the row is on the advance's Purchase Order, in the unit
	the advance is counted in, and books exactly the quantity the advance covers. Quantity beyond
	the advance goes on its own row, without an advance."""
	if parc.docstatus == 1:
		return _("was already used by Purchase Receipt {0}").format(parc.purchase_receipt)
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
	covered, booked = flt(parc.get(EXPECTED)), flt(row.qty)
	if booked - covered > QTY_EPSILON:
		return _(
			"covers {0:.3f}; this row books {1:.3f}. Book the quantity beyond the advance on its own "
			"row, without an advance"
		).format(covered, booked)
	if covered - booked > QTY_EPSILON:
		return _("covers {0:.3f}; this row books only {1:.3f}. An advance is used whole").format(
			covered, booked
		)
	return None


def _named_advances(doc, for_update=False):
	"""[(PARC, row)] for every advance the receipt's rows name, or ParcRefusedError listing every
	row that cannot use its advance.

	With `for_update` each named PARC is re-read under a row lock held until the transaction ends.
	A locking read sees the latest committed row, so a PARC that another receipt has just closed
	shows as used. The PARC locks are taken in name order. This does not rule out deadlocks: on
	submit, ERPNext's own posting runs first and locks Purchase Order, stock and ledger rows, and
	can still end in a deadlock or lock wait timeout, which the caller may retry."""
	rows = sorted((d for d in doc.get("items") if d.get(PARC_FIELD)), key=lambda d: d.get(PARC_FIELD))
	if rows and doc.get("is_return"):
		frappe.throw(
			_("A return cannot use an advance (PARC); clear Advance (PARC) on its rows."), ParcRefusedError
		)
	named, errors, seen = [], [], set()
	for row in rows:
		name = row.get(PARC_FIELD)
		where = _("Row {0}: advance {1}").format(row.idx, name)
		if name in seen:
			errors.append((row.idx, _("{0} is named on more than one row").format(where)))
		elif not frappe.db.exists(PARC, name):
			errors.append((row.idx, _("{0} does not exist").format(where)))
		else:
			parc = frappe.get_doc(PARC, name, for_update=for_update)
			supplier = parc.purchase_order and frappe.db.get_value(
				"Purchase Order", parc.purchase_order, "supplier"
			)
			reason = advance_refusal(parc, row, doc.supplier, supplier)
			if reason:
				errors.append((row.idx, f"{where} {reason}"))
			else:
				named.append((parc, row))
		seen.add(name)
	if errors:
		message = "<br>".join(text for _idx, text in sorted(errors))
		frappe.throw(message, ParcRefusedError, title=_("Advance (PARC) refused"))
	return named


# ---- Purchase Receipt: Validate -> refuse on save what the submit would refuse -------------
def check_named_parcs_on_purchase_receipt(doc, method=None):
	_named_advances(doc)


# ---- Purchase Receipt: On Submit -> close exactly the advances the receipt names -----------
def close_named_parcs_on_purchase_receipt(doc, method=None):
	# Runs after ERPNext's own Purchase Receipt on_submit (Frappe calls the controller first, then
	# doc_events). The check is repeated on a locked re-read: of two receipts naming one advance,
	# the later one is refused as already used, even when both passed the save check.
	for parc, row in _named_advances(doc, for_update=True):
		parc.purchase_receipt = doc.name
		parc.qty_of_pr = row.qty
		parc.grand_total_of_pr = doc.grand_total
		# Received-to-date already includes this receipt (docstatus is 1 by on_submit).
		parc.qty_left_to_be_received_from_po = flt(parc.qty_of_po) - _received_qty(parc.purchase_order)
		parc.flags.ignore_permissions = True
		parc.submit()
		frappe.msgprint(
			_("Advance {0} used by this receipt").format(parc.name), alert=True, indicator="green"
		)


# ---- Purchase Receipt: On Cancel -> cancel the closed PARCs and re-open their advances -----
def reopen_parc_on_purchase_receipt_cancel(doc, method=None):
	# Runs before Frappe's back-link check, so cancelling the PARC here is what lets the
	# Purchase Receipt cancel at all.
	for name in frappe.get_all(PARC, filters={"purchase_receipt": doc.name, "docstatus": 1}, pluck="name"):
		parc = frappe.get_doc(PARC, name)
		parc.flags.ignore_permissions = True
		parc.cancel()
		draft = frappe.copy_doc(parc)
		draft.docstatus = 0  # copy_doc keeps the source's (now cancelled) docstatus
		draft.purchase_receipt = None
		draft.qty_of_pr = None
		draft.grand_total_of_pr = None
		draft.qty_left_to_be_received_from_po = (
			flt(parc.qty_of_po) - flt(parc.get(EXPECTED)) - _received_qty(parc.purchase_order)
		)
		draft.insert(ignore_permissions=True)


# ---- Lookup for the GRN approval screen ---------------------------------------------------
@frappe.whitelist(methods=["GET"])
def get_open_advances(supplier: str):
	"""A supplier's open (draft) advances, oldest payment first: by the Payment Entry's posting
	date, then the order the payments were entered, then PARC name. The name only separates the
	advances of one payment split across Purchase Orders and has no business meaning: it is
	creation order under the site's PARC naming rule, a random hash without one. The order is a
	suggestion; the receipt check does not enforce it. Read-only."""
	frappe.has_permission(PARC, "read", throw=True)
	parc = frappe.qb.DocType(PARC)
	po = frappe.qb.DocType("Purchase Order")
	pe = frappe.qb.DocType("Payment Entry")
	return (
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
			parc.advance_paid,
			parc.field(EXPECTED),
			parc.uom_of_item,
		)
		.where((parc.docstatus == 0) & (pe.docstatus == 1) & (po.supplier == supplier))
		.orderby(pe.posting_date)
		.orderby(pe.creation)
		.orderby(parc.name)
		.run(as_dict=True)
	)
