# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Advances used up across receipts, through real documents, on a site with ERPNext and this app.

Each test makes a supplier of its own (a copy of the site's latest submitted Purchase Order's
supplier), so the site's real advances never queue ahead of the test's. It builds Purchase Orders by
copying that latest Purchase Order (so the company's mandatory custom fields come along), pays
advances against them with Payment Entries, and books Purchase Receipts whose rows name those
advances. Nothing is kept: FrappeTestCase rolls the class back, and each refused save, submit or
cancel is rolled back to a savepoint, as a request would be.

Needs a submitted Purchase Order to copy, a default bank or cash account on its company (for the
payments), a fiscal year covering the last ten days, and the legacy PARC Server Scripts disabled.
Run it on a lab or staging site, never on production: it creates real documents and holds their
locks while it runs.

Two receipts submitted at the same time are covered on one connection only: one test skips the
save check so that the submit's own locked re-read has to refuse the second receipt. A race across
two connections is not tested, because a second connection cannot see this suite's uncommitted
documents.

    bench --site <site> execute fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.test_parc_named_advances.run
"""

import sys
import unittest
from unittest import mock

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt, update_status
from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_return
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, getdate, nowdate

from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control import (
	purchase_advance_receipt_control as parc_module,
)
from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control import (
	CONSUMPTIONS,
	EXPECTED,
	PARC,
	PARC_FIELD,
	ParcRefusedError,
	get_open_advances,
	get_open_purchase_orders,
	remaining_qty,
)
from fuelbuddy_procurement.install import LEGACY_SERVER_SCRIPTS, create_parc_fields

_ROW_KEYS_NOT_COPIED = ("name", "idx", "parent", "parentfield", "parenttype", "docstatus", "__islocal")
_SUPPLIER_CONTACT_FIELDS = (
	"supplier_address",
	"address_display",
	"contact_person",
	"contact_display",
	"contact_mobile",
	"contact_email",
)


def _latest_po():
	name = frappe.db.get_value("Purchase Order", {"docstatus": 1}, "name", order_by="creation desc")
	if not name:
		raise unittest.SkipTest("needs a submitted Purchase Order to copy")
	return frappe.get_doc("Purchase Order", name)


def _new_supplier(template):
	"""A supplier of the test's own: a copy of `template`'s supplier under a new name."""
	supplier = frappe.copy_doc(frappe.get_doc("Supplier", template.supplier), ignore_no_copy=False)
	supplier.supplier_name = f"PARC test {frappe.generate_hash(length=10)}"
	supplier.payment_terms = None
	supplier.on_hold = supplier.disabled = 0
	supplier.set("portal_users", [])
	supplier.insert()
	return supplier.name


def _new_po(template, supplier, qty=1000.0, rate=10.0, days_ago=0, terms=None):
	"""Submitted one-line copy of `template` for `supplier`: `qty` at `rate`, dated `days_ago`, on
	payment terms template `terms` (none by default)."""
	po = frappe.copy_doc(template, ignore_no_copy=False)  # clears advance_paid, per_received, ...
	po.naming_series = template.naming_series
	po.docstatus = 0
	po.supplier = supplier
	po.supplier_name = frappe.db.get_value("Supplier", supplier, "supplier_name")
	for field in _SUPPLIER_CONTACT_FIELDS:  # they belong to the template's supplier
		po.set(field, None)
	po.transaction_date = po.schedule_date = add_days(nowdate(), -days_ago)
	# An advance here is a share of the whole PO. Payment terms that allocate by term make
	# get_payment_entry ignore party_amount, so the copy has none, nor the supplier's default.
	po.payment_terms_template = terms
	po.ignore_default_payment_terms_template = 1
	po.set("payment_schedule", [])  # rebuilt from `terms`, or one row for the whole amount
	po.set("items", po.items[:1])
	po.items[0].update({"qty": qty, "rate": rate, "schedule_date": po.schedule_date})
	po.insert()
	po.submit()
	return po


def _terms_template():
	"""A payment terms template of two 50% terms that allocates payments by term."""
	suffix = frappe.generate_hash(length=8)
	terms = []
	for label, days in (("now", 0), ("later", 30)):
		term = {"invoice_portion": 50, "due_date_based_on": "Day(s) after invoice date", "credit_days": days}
		name = f"PARC test {label} {suffix}"
		frappe.get_doc({"doctype": "Payment Term", "payment_term_name": name, **term}).insert()
		terms.append({"payment_term": name, **term})
	return (
		frappe.get_doc(
			{
				"doctype": "Payment Terms Template",
				"template_name": f"PARC test 50-50 {suffix}",
				"allocate_payment_based_on_payment_terms": 1,
				"terms": terms,
			}
		)
		.insert()
		.name
	)


def _pay(po, share=None, days_ago=0):
	"""Pays `share` of the PO's grand total (what is outstanding when None), posted `days_ago`.
	Returns the advances (PARCs) the payment opened, by name: one per reference row."""
	amount = flt(po.grand_total * share, 2) if share else None
	pe = get_payment_entry("Purchase Order", po.name, party_amount=amount)
	pe.posting_date = pe.reference_date = add_days(nowdate(), -days_ago)
	pe.reference_no = f"PARC test {po.name}"
	pe.insert()
	pe.submit()
	names = frappe.get_all(PARC, filters={"payment_entry": pe.name}, pluck="name", order_by="name")
	return [frappe.get_doc(PARC, name) for name in names]


def _covered(parc):
	return flt(parc.get(EXPECTED))


def _left(parc):
	return remaining_qty(frappe.get_doc(PARC, parc.name))


def _uses(parc):
	"""The advance's consumption rows as (receipt, qty, active)."""
	return [(row.purchase_receipt, flt(row.qty, 3), row.is_active) for row in parc.get(CONSUMPTIONS)]


def _receipt(*rows):
	"""Draft Purchase Receipt with one row per (Purchase Order, qty, advance name or None)."""
	receipt, base_rows = None, {}
	for po, _qty, _advance in rows:
		if po.name not in base_rows:
			mapped = make_purchase_receipt(po.name)
			receipt = receipt or mapped
			base = mapped.items[0].as_dict()
			for key in _ROW_KEYS_NOT_COPIED:
				base.pop(key, None)
			base_rows[po.name] = base
	receipt.set("items", [])
	for po, qty, advance in rows:
		receipt.append("items", {**base_rows[po.name], "qty": qty, "received_qty": qty, PARC_FIELD: advance})
	return receipt


def _submit(receipt):
	receipt.insert()
	receipt.submit()
	return receipt


def _state(parc):
	return frappe.db.get_value(PARC, parc.name, ["docstatus", "purchase_receipt"])


def _open_copy(parc):
	"""The draft that re-opened `parc`: same payment and Purchase Order, still open."""
	name = frappe.db.get_value(
		PARC,
		{"payment_entry": parc.payment_entry, "purchase_order": parc.purchase_order, "docstatus": 0},
		"name",
	)
	return frappe.get_doc(PARC, name)


class TestParcPartialAdvances(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		enabled = [
			s for s in LEGACY_SERVER_SCRIPTS if frappe.db.get_value("Server Script", s, "disabled") == 0
		]
		if enabled:
			raise AssertionError(f"Disable the legacy PARC Server Scripts first: {enabled}")
		if not frappe.get_meta("Purchase Receipt Item").has_field(PARC_FIELD):
			create_parc_fields()  # the app's own field; its ALTER TABLE commits before any test data
		cls.template = _latest_po()

	def setUp(self):
		self.supplier = _new_supplier(self.template)

	def po(self, **kwargs):
		return _new_po(self.template, self.supplier, **kwargs)

	def assertRefused(self, action, *fragments, exc=ParcRefusedError):
		"""`action` raises `exc` mentioning every fragment; its writes are rolled back."""
		frappe.db.savepoint("parc_refused")
		try:
			with self.assertRaises(exc) as ctx:
				action()
		finally:
			frappe.db.rollback(save_point="parc_refused")
		for fragment in fragments:
			self.assertIn(fragment, str(ctx.exception))

	def test_receipt_row_field_is_installed(self):
		field = frappe.get_meta("Purchase Receipt Item").get_field(PARC_FIELD)
		self.assertEqual((field.fieldtype, field.options, field.no_copy), ("Link", PARC, 1))

	def test_payment_opens_an_advance_for_its_share_of_the_po(self):
		po = self.po(qty=1000.0)
		(parc,) = _pay(po, 0.25)
		self.assertEqual(
			(parc.docstatus, parc.purchase_order, parc.uom_of_item), (0, po.name, po.items[0].uom)
		)
		self.assertAlmostEqual(_covered(parc), 250.0, delta=0.01)
		self.assertEqual((flt(parc.qty_consumed), _uses(parc)), (0.0, []))
		self.assertAlmostEqual(flt(parc.qty_remaining), 250.0, delta=0.01)

	def test_receipts_use_an_advance_up_bit_by_bit_and_the_last_one_closes_it(self):
		po = self.po(qty=1000.0)
		(parc,) = _pay(po, 0.4)  # 400
		first = _submit(_receipt((po, 150.0, parc.name)))
		parc.reload()
		self.assertEqual((parc.docstatus, _uses(parc)), (0, [(first.name, 150.0, 1)]))
		self.assertAlmostEqual(flt(parc.qty_remaining), 250.0, delta=0.01)
		last = _submit(_receipt((po, 250.0, parc.name)))
		parc.reload()
		self.assertEqual((parc.docstatus, parc.purchase_receipt), (1, last.name))
		self.assertEqual(_uses(parc), [(first.name, 150.0, 1), (last.name, 250.0, 1)])
		self.assertAlmostEqual(flt(parc.qty_of_pr), 400.0, delta=0.01)
		self.assertAlmostEqual(flt(parc.qty_remaining), 0.0, delta=0.01)
		self.assertAlmostEqual(flt(parc.qty_left_to_be_received_from_po), 600.0, delta=0.01)
		self.assertEqual(get_open_advances(self.supplier), [])

	def test_the_rest_of_a_receipt_goes_on_rows_without_an_advance(self):
		po = self.po(qty=1000.0)
		(parc,) = _pay(po, 0.4)
		pr = _submit(_receipt((po, 400.0, parc.name), (po, 50.0, None)))
		parc.reload()
		self.assertEqual((parc.docstatus, parc.purchase_receipt), (1, pr.name))
		self.assertAlmostEqual(flt(parc.qty_of_pr), 400.0, delta=0.01)
		self.assertAlmostEqual(flt(parc.qty_left_to_be_received_from_po), 550.0, delta=0.01)

	def test_a_receipt_naming_no_advance_books_nothing(self):
		po = self.po()
		(parc,) = _pay(po, 0.4)
		_submit(_receipt((po, 400.0, None)))
		parc.reload()
		self.assertEqual((parc.docstatus, _uses(parc)), (0, []))

	def test_a_row_may_not_book_more_than_the_advance_has_left(self):
		po = self.po()
		(parc,) = _pay(po, 0.4)
		first = _submit(_receipt((po, 300.0, parc.name)))
		self.assertRefused(
			_receipt((po, 100.5, parc.name)).insert,
			"has 100.000",
			"this row books 100.500",
			f"last used by Purchase Receipt {first.name}",
		)
		_submit(_receipt((po, 100.0, parc.name)))  # exactly what is left
		self.assertEqual(_state(parc)[0], 1)

	def test_oldest_advance_first_is_enforced(self):
		po_new, po_old = self.po(), self.po()
		(newer,) = _pay(po_new, 0.1, days_ago=1)  # 100 each
		(older,) = _pay(po_old, 0.1, days_ago=3)
		self.assertRefused(
			_receipt((po_new, 50.0, newer.name)).insert,
			f"advance {newer.name} is not the oldest advance of {self.supplier} with quantity left",
			f"use {older.name} first (100.000",
			f"on Purchase Order {po_old.name}",
		)
		_submit(_receipt((po_old, 60.0, older.name)))  # part of it: still first in line
		self.assertRefused(_receipt((po_new, 50.0, newer.name)).insert, f"use {older.name} first (40.000")
		_submit(_receipt((po_old, 40.0, older.name)))  # the rest: the older advance is used up
		_submit(_receipt((po_new, 50.0, newer.name)))
		self.assertEqual(_state(older)[0], 1)
		self.assertAlmostEqual(_left(newer), 50.0, delta=0.01)

	def test_payments_on_the_same_day_go_in_the_order_they_were_entered(self):
		po_1, po_2 = self.po(), self.po()
		(first,) = _pay(po_1, 0.1, days_ago=2)
		(second,) = _pay(po_2, 0.1, days_ago=2)
		self.assertRefused(_receipt((po_2, 50.0, second.name)).insert, f"use {first.name} first")
		_submit(_receipt((po_1, 50.0, first.name)))

	def test_one_payment_on_two_payment_terms_opens_two_advances_used_one_after_the_other(self):
		po = self.po(qty=1000.0, terms=_terms_template())
		advances = _pay(po)  # the whole PO: one reference row per term
		self.assertEqual([round(_covered(parc), 3) for parc in advances], [500.0, 500.0])
		first, second = advances  # one payment: by name
		self.assertEqual([row.name for row in get_open_advances(self.supplier)], [first.name, second.name])
		self.assertRefused(_receipt((po, 100.0, second.name)).insert, f"use {first.name} first (500.000")
		_submit(_receipt((po, 300.0, first.name)))
		self.assertRefused(_receipt((po, 300.0, first.name)).insert, "has 200.000")
		self.assertRefused(
			_receipt((po, 100.0, first.name), (po, 100.0, second.name)).insert,
			f"advance {second.name} cannot be used as well",
		)
		last = _submit(_receipt((po, 200.0, first.name), (po, 100.0, None)))
		self.assertEqual(_state(first), (1, last.name))
		_submit(_receipt((po, 400.0, second.name)))
		self.assertAlmostEqual(_left(second), 100.0, delta=0.01)
		self.assertEqual([row.name for row in get_open_advances(self.supplier)], [second.name])

	def test_advance_on_another_purchase_order_is_refused(self):
		po, other = self.po(), self.po()
		(parc,) = _pay(po, 0.4)
		self.assertRefused(_receipt((other, 100.0, parc.name)).insert, po.name, other.name)

	def test_advance_to_another_supplier_is_refused(self):
		po = self.po()
		(parc,) = _pay(po, 0.4)
		other = _new_po(self.template, _new_supplier(self.template))
		self.assertRefused(_receipt((other, 100.0, parc.name)).insert, f"supplier {self.supplier}")

	def test_same_advance_on_two_rows_is_refused(self):
		po = self.po()
		(parc,) = _pay(po, 0.4)
		self.assertRefused(
			_receipt((po, 100.0, parc.name), (po, 100.0, parc.name)).insert, "more than one row"
		)

	def test_a_later_receipt_is_refused_once_an_earlier_one_used_what_it_needs(self):
		"""One after the other: the second submit is refused by the save check it runs first."""
		po = self.po()
		(parc,) = _pay(po, 0.4)
		first, second = _receipt((po, 300.0, parc.name)), _receipt((po, 300.0, parc.name))
		first.insert()
		second.insert()  # both fit while nothing is used
		first.submit()
		self.assertRefused(second.submit, "has 100.000", f"last used by Purchase Receipt {first.name}")
		self.assertAlmostEqual(_left(parc), 100.0, delta=0.01)

	def test_submit_rechecks_under_lock_what_the_save_check_let_through(self):
		"""At the same time: both receipts pass the save check while the advance has 400 left.
		Skipping that check on the second submit leaves the submit's locked re-read to refuse it."""
		po = self.po()
		(parc,) = _pay(po, 0.4)
		first, second = _receipt((po, 300.0, parc.name)), _receipt((po, 300.0, parc.name))
		first.insert()
		second.insert()
		first.submit()
		save_check = mock.Mock(return_value=None)
		with mock.patch.object(parc_module, "check_named_parcs_on_purchase_receipt", save_check):
			self.assertRefused(second.submit, "has 100.000", f"last used by Purchase Receipt {first.name}")
		save_check.assert_called()  # the save check really was skipped, so the refusal came on submit
		parc.reload()
		self.assertEqual((parc.docstatus, _uses(parc)), (0, [(first.name, 300.0, 1)]))

	def test_cancel_gives_the_quantity_back_to_an_open_advance(self):
		po = self.po()
		(parc,) = _pay(po, 0.4)
		first = _submit(_receipt((po, 150.0, parc.name)))
		second = _submit(_receipt((po, 100.0, parc.name)))
		first.cancel()
		parc.reload()
		self.assertEqual(parc.docstatus, 0)
		self.assertEqual(_uses(parc), [(first.name, 150.0, 0), (second.name, 100.0, 1)])
		self.assertAlmostEqual(flt(parc.qty_remaining), 300.0, delta=0.01)

	def test_cancelling_the_receipt_that_closed_an_advance_reopens_it(self):
		po = self.po()
		(parc,) = _pay(po, 0.4)
		first = _submit(_receipt((po, 300.0, parc.name)))
		last = _submit(_receipt((po, 100.0, parc.name)))
		last.cancel()
		self.assertEqual(_state(parc)[0], 2)
		reopened = _open_copy(parc)
		self.assertNotEqual(reopened.name, parc.name)
		self.assertEqual(
			(reopened.purchase_receipt, flt(reopened.qty_of_pr)), (None, 0.0)
		)  # Float: 0 when empty
		self.assertEqual(_uses(reopened), [(first.name, 300.0, 1), (last.name, 100.0, 0)])
		self.assertAlmostEqual(flt(reopened.qty_remaining), 100.0, delta=0.01)
		_submit(_receipt((po, 100.0, reopened.name)))  # the re-opened advance can be used again
		self.assertEqual(_state(reopened)[0], 1)

	def test_cancelling_an_earlier_receipt_reopens_a_used_up_advance_in_its_old_place(self):
		po = self.po()
		(parc,) = _pay(po, 0.4)
		(newer,) = _pay(po, 0.1)  # same day, entered later: queued behind
		first = _submit(_receipt((po, 300.0, parc.name)))
		last = _submit(_receipt((po, 100.0, parc.name)))
		self.assertEqual(_state(parc), (1, last.name))
		first.cancel()
		self.assertEqual(_state(parc)[0], 2)
		reopened = _open_copy(parc)
		self.assertEqual((reopened.payment_entry, reopened.purchase_order), (parc.payment_entry, po.name))
		self.assertEqual(_uses(reopened), [(first.name, 300.0, 0), (last.name, 100.0, 1)])
		self.assertAlmostEqual(flt(reopened.qty_remaining), 300.0, delta=0.01)
		self.assertEqual([row.name for row in get_open_advances(self.supplier)], [reopened.name, newer.name])
		last.cancel()  # the receipt that closed it can still be cancelled; its quantity comes back too
		reopened.reload()
		self.assertAlmostEqual(flt(reopened.qty_remaining), 400.0, delta=0.01)

	def test_payment_cannot_be_cancelled_while_a_receipt_uses_its_advance(self):
		po = self.po()
		(parc,) = _pay(po, 0.4)

		def cancel_payment():
			frappe.get_doc("Payment Entry", parc.payment_entry).cancel()

		receipt = _receipt((po, 100.0, parc.name))
		receipt.insert()  # a draft naming the advance
		in_use = f"{parc.name} is in use by Purchase Receipt {receipt.name}"
		self.assertRefused(cancel_payment, in_use, exc=frappe.LinkExistsError)
		receipt.submit()  # has used part of it
		self.assertRefused(cancel_payment, in_use, exc=frappe.LinkExistsError)
		receipt.cancel()
		cancel_payment()  # nothing uses it any more: the payment's draft advance goes with it
		self.assertFalse(frappe.db.exists(PARC, parc.name))

	def test_returns_neither_use_nor_give_back_an_advance(self):
		po = self.po()
		(parc,) = _pay(po, 0.4)
		pr = _submit(_receipt((po, 300.0, parc.name)))
		ret, naming = make_purchase_return(pr.name), make_purchase_return(pr.name)
		naming.items[0].set(PARC_FIELD, parc.name)
		self.assertRefused(naming.insert, "return")
		self.assertFalse(ret.items[0].get(PARC_FIELD))  # the field is not copied onto a return
		_submit(ret)
		self.assertAlmostEqual(_left(parc), 100.0, delta=0.01)

	def test_open_advances_come_oldest_first_with_what_each_has_left(self):
		po_1, po_2 = self.po(), self.po()
		(newest,) = _pay(po_1, 0.1, days_ago=1)
		(oldest,) = _pay(po_2, 0.1, days_ago=3)
		(middle,) = _pay(po_1, 0.1, days_ago=2)
		_submit(_receipt((po_2, 40.0, oldest.name)))
		rows = get_open_advances(self.supplier)
		self.assertEqual([row.name for row in rows], [oldest.name, middle.name, newest.name])
		first = rows[0]
		self.assertEqual((first.purchase_order, first.payment_entry), (po_2.name, oldest.payment_entry))
		self.assertEqual(getdate(first.payment_date), getdate(add_days(nowdate(), -3)))
		self.assertAlmostEqual(flt(first.get(EXPECTED)), 100.0, delta=0.01)
		self.assertAlmostEqual(flt(first.qty_consumed), 40.0, delta=0.01)
		self.assertAlmostEqual(flt(first.qty_remaining), 60.0, delta=0.01)
		self.assertEqual((first.company, first.po_status), (po_2.company, "To Receive and Bill"))
		self.assertNotIn("payment_created", first)
		_submit(_receipt((po_2, 60.0, oldest.name)))
		self.assertEqual([row.name for row in get_open_advances(self.supplier)], [middle.name, newest.name])
		self.assertEqual(get_open_advances(self.supplier, company="PARC test: no such company"), [])
		self.assertEqual(get_open_advances("PARC test: no such supplier"), [])

	def test_an_advance_on_a_closed_order_stays_first_in_line(self):
		po_old, po_new = self.po(), self.po()
		(older,) = _pay(po_old, 0.1, days_ago=2)
		(newer,) = _pay(po_new, 0.1, days_ago=1)
		update_status("Closed", po_old.name)
		self.assertRefused(
			_receipt((po_new, 50.0, newer.name)).insert,
			f"use {older.name} first",
			f"Purchase Order {po_old.name} is Closed, so no receipt can be booked against it",
		)
		first = get_open_advances(self.supplier)[0]
		self.assertEqual((first.name, first.po_status), (older.name, "Closed"))

	def test_open_purchase_orders_are_suggested_oldest_first(self):
		newer = self.po(days_ago=1)
		older = self.po(days_ago=5)
		received = self.po(days_ago=7)
		closed = self.po(days_ago=9)
		update_status("Closed", closed.name)
		_submit(_receipt((received, 1000.0, None)))  # nothing left to receive
		_submit(_receipt((older, 300.0, None)))
		rows = get_open_purchase_orders(self.supplier)
		self.assertEqual([row.purchase_order for row in rows], [older.name, newer.name])
		self.assertAlmostEqual(flt(rows[0].qty_to_receive), 700.0, delta=0.01)
		self.assertAlmostEqual(flt(rows[1].qty_to_receive), 1000.0, delta=0.01)
		self.assertEqual(get_open_purchase_orders(self.supplier, company="PARC test: no such company"), [])


def run():
	"""Entry point for ``bench --site <site> execute <this module>.run``; nothing is committed."""
	frappe.flags.in_test = True
	suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	frappe.db.rollback()
	return {
		"ran": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
		"skipped": len(result.skipped),
	}
