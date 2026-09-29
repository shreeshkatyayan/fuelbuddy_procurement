# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Named advances through real documents, on a site with ERPNext and this app installed.

Each test builds its own Purchase Orders by copying the site's latest submitted one (so the
company's mandatory custom fields come along), pays advances against them with Payment Entries,
and books Purchase Receipts whose rows name those advances. Nothing is kept: FrappeTestCase rolls
the class back, and each refused save, submit or cancel is rolled back to a savepoint, as a
request would be.

Needs a submitted Purchase Order to copy, a default bank or cash account on its company (for the
payments), and the legacy PARC Server Scripts disabled. The other-supplier test also needs a
submitted Purchase Order of a second supplier and is skipped without one. Run it on a lab or
staging site, never on production: it creates real documents and holds their locks while it runs.

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
from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_return
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, getdate, nowdate

from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control import (
	purchase_advance_receipt_control as parc_module,
)
from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control import (
	EXPECTED,
	PARC,
	PARC_FIELD,
	ParcRefusedError,
	get_open_advances,
)
from fuelbuddy_procurement.install import LEGACY_SERVER_SCRIPTS, create_parc_fields

_ROW_KEYS_NOT_COPIED = ("name", "idx", "parent", "parentfield", "parenttype", "docstatus", "__islocal")


def _latest_po(other_than=None):
	filters = {"docstatus": 1}
	if other_than:
		filters["supplier"] = ["!=", other_than]
	name = frappe.db.get_value("Purchase Order", filters, "name", order_by="creation desc")
	if not name:
		why = f" of a supplier other than {other_than}" if other_than else ""
		raise unittest.SkipTest(f"needs a submitted Purchase Order{why} to copy")
	return frappe.get_doc("Purchase Order", name)


def _new_po(template, qty=1000.0, rate=10.0):
	"""Submitted one-line copy of `template` for `qty` at `rate`, dated today."""
	po = frappe.copy_doc(template, ignore_no_copy=False)  # clears advance_paid, per_received, ...
	po.naming_series = template.naming_series
	po.docstatus = 0
	po.transaction_date = po.schedule_date = nowdate()
	po.set("items", po.items[:1])
	po.items[0].update({"qty": qty, "rate": rate, "schedule_date": nowdate()})
	po.insert()
	po.submit()
	return po


def _pay(po, share, days_ago=0):
	"""Advance of `share` of the PO's grand total, posted `days_ago`; returns the PARC it opens."""
	pe = get_payment_entry("Purchase Order", po.name, party_amount=flt(po.grand_total * share, 2))
	pe.posting_date = pe.reference_date = add_days(nowdate(), -days_ago)
	pe.reference_no = f"PARC test {po.name}"
	pe.insert()
	pe.submit()
	return frappe.get_doc(PARC, frappe.db.get_value(PARC, {"payment_entry": pe.name}, "name"))


def _covered(parc):
	return flt(parc.get(EXPECTED))


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


def _state(parc):
	return frappe.db.get_value(PARC, parc.name, ["docstatus", "purchase_receipt"])


class TestParcNamedAdvances(FrappeTestCase):
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

	def assertOpen(self, parc):
		self.assertEqual(_state(parc), (0, None))

	def test_receipt_row_field_is_installed(self):
		field = frappe.get_meta("Purchase Receipt Item").get_field(PARC_FIELD)
		self.assertEqual((field.fieldtype, field.options, field.no_copy), ("Link", PARC, 1))

	def test_payment_opens_an_advance_for_its_share_of_the_po(self):
		po = _new_po(self.template, qty=1000.0)
		parc = _pay(po, 0.25)
		self.assertEqual(
			(parc.docstatus, parc.purchase_order, parc.uom_of_item), (0, po.name, po.items[0].uom)
		)
		self.assertAlmostEqual(_covered(parc), 250.0, delta=0.01)

	def test_receipt_closes_exactly_the_advance_it_names(self):
		po = _new_po(self.template, qty=1000.0)
		older, named = _pay(po, 0.3, days_ago=2), _pay(po, 0.2, days_ago=1)
		pr = _receipt((po, _covered(named), named.name), (po, 7.0, None))  # 7 beyond the advance
		pr.insert()
		pr.submit()
		named.reload()
		self.assertEqual((named.docstatus, named.purchase_receipt), (1, pr.name))
		self.assertAlmostEqual(flt(named.qty_of_pr), _covered(named), delta=0.001)
		self.assertAlmostEqual(
			flt(named.qty_left_to_be_received_from_po), 1000.0 - _covered(named) - 7.0, delta=0.01
		)
		self.assertOpen(older)  # oldest-first is not enforced: the receipt decides which advance is used

	def test_receipt_naming_nothing_closes_nothing(self):
		po = _new_po(self.template)
		parc = _pay(po, 0.4)
		pr = _receipt((po, _covered(parc), None))  # exactly the advance's qty, but not named
		pr.insert()
		pr.submit()
		self.assertOpen(parc)

	def test_one_receipt_can_use_several_advances_on_several_orders(self):
		po_a, po_b = _new_po(self.template), _new_po(self.template)
		a1, a2, b1 = _pay(po_a, 0.2), _pay(po_a, 0.3), _pay(po_b, 0.5)
		pr = _receipt(
			(po_a, _covered(a1), a1.name), (po_a, _covered(a2), a2.name), (po_b, _covered(b1), b1.name)
		)
		pr.insert()
		pr.submit()
		for parc in (a1, a2, b1):
			self.assertEqual(_state(parc), (1, pr.name))

	def test_quantity_must_be_exactly_what_the_advance_covers(self):
		po = _new_po(self.template)
		parc = _pay(po, 0.4)
		self.assertRefused(_receipt((po, _covered(parc) + 1, parc.name)).insert, "beyond the advance")
		self.assertRefused(_receipt((po, _covered(parc) - 1, parc.name)).insert, "used whole")
		self.assertOpen(parc)

	def test_advance_on_another_purchase_order_is_refused(self):
		po, other = _new_po(self.template), _new_po(self.template)
		parc = _pay(po, 0.4)
		self.assertRefused(_receipt((other, _covered(parc), parc.name)).insert, po.name, other.name)

	def test_advance_to_another_supplier_is_refused(self):
		po = _new_po(self.template)
		parc = _pay(po, 0.4)
		other = _new_po(_latest_po(other_than=po.supplier))
		self.assertRefused(_receipt((other, _covered(parc), parc.name)).insert, f"supplier {po.supplier}")

	def test_same_advance_on_two_rows_is_refused(self):
		po = _new_po(self.template)
		parc = _pay(po, 0.4)
		half = _covered(parc) / 2
		self.assertRefused(_receipt((po, half, parc.name), (po, half, parc.name)).insert, "more than one row")

	def test_of_two_receipts_naming_one_advance_the_second_is_refused(self):
		"""One after the other: the second submit is refused by the save check it runs first."""
		po = _new_po(self.template)
		parc = _pay(po, 0.4)
		first, second = _receipt((po, _covered(parc), parc.name)), _receipt((po, _covered(parc), parc.name))
		first.insert()
		second.insert()  # two drafts may name it; the first submit uses it
		first.submit()
		self.assertRefused(second.submit, "already used", first.name)
		self.assertRefused(_receipt((po, _covered(parc), parc.name)).insert, "already used", first.name)
		self.assertEqual(_state(parc), (1, first.name))

	def test_submit_rechecks_under_lock_what_the_save_check_let_through(self):
		"""At the same time: both receipts pass the save check while the advance is still open.
		Skipping that check on the second submit leaves the submit's locked re-read to refuse it."""
		po = _new_po(self.template)
		parc = _pay(po, 0.4)
		first, second = _receipt((po, _covered(parc), parc.name)), _receipt((po, _covered(parc), parc.name))
		first.insert()
		second.insert()
		first.submit()
		save_check = mock.Mock(return_value=None)
		with mock.patch.object(parc_module, "check_named_parcs_on_purchase_receipt", save_check):
			self.assertRefused(second.submit, "already used", first.name)
		save_check.assert_called()  # the save check really was skipped, so the refusal came on submit
		self.assertEqual(_state(parc), (1, first.name))

	def test_cancel_reopens_exactly_the_advances_the_receipt_used(self):
		po = _new_po(self.template)
		used, untouched = _pay(po, 0.3), _pay(po, 0.2)
		pr = _receipt((po, _covered(used), used.name))
		pr.insert()
		pr.submit()
		pr.cancel()
		self.assertEqual(_state(used)[0], 2)
		reopened = frappe.get_all(
			PARC,
			filters={"payment_entry": used.payment_entry, "docstatus": 0},
			fields=["purchase_order", "purchase_receipt", EXPECTED],
		)
		self.assertEqual([(r.purchase_order, r.purchase_receipt) for r in reopened], [(po.name, None)])
		self.assertAlmostEqual(flt(reopened[0].get(EXPECTED)), _covered(used), delta=0.001)
		self.assertOpen(untouched)

	def test_payment_whose_advance_a_draft_receipt_names_cannot_be_cancelled(self):
		po = _new_po(self.template)
		parc = _pay(po, 0.4)
		_receipt((po, _covered(parc), parc.name)).insert()
		pe = frappe.get_doc("Payment Entry", parc.payment_entry)
		self.assertRefused(pe.cancel, parc.name, exc=frappe.LinkExistsError)
		self.assertOpen(parc)

	def test_returns_neither_use_nor_reopen_advances(self):
		po = _new_po(self.template)
		used, other = _pay(po, 0.3), _pay(po, 0.2)
		pr = _receipt((po, _covered(used), used.name))
		pr.insert()
		pr.submit()
		ret, naming = make_purchase_return(pr.name), make_purchase_return(pr.name)
		naming.items[0].set(PARC_FIELD, other.name)
		self.assertRefused(naming.insert, "return")
		self.assertFalse(ret.items[0].get(PARC_FIELD))  # the field is not copied onto a return
		ret.insert()
		ret.submit()
		self.assertEqual(_state(used), (1, pr.name))
		self.assertOpen(other)

	def test_open_advances_come_oldest_payment_first(self):
		po_1, po_2 = _new_po(self.template), _new_po(self.template)
		newest = _pay(po_1, 0.1, days_ago=1)
		oldest = _pay(po_2, 0.1, days_ago=3)
		middle = _pay(po_1, 0.1, days_ago=2)
		used = _pay(po_2, 0.1, days_ago=4)
		pr = _receipt((po_2, _covered(used), used.name))
		pr.insert()
		pr.submit()
		rows = get_open_advances(po_1.supplier)
		ours = {newest.name, oldest.name, middle.name, used.name}
		self.assertEqual([r.name for r in rows if r.name in ours], [oldest.name, middle.name, newest.name])
		first = next(r for r in rows if r.name == oldest.name)
		self.assertEqual(first.purchase_order, po_2.name)
		self.assertEqual(getdate(first.payment_date), getdate(add_days(nowdate(), -3)))
		self.assertAlmostEqual(flt(first.get(EXPECTED)), _covered(oldest), delta=0.001)
		self.assertEqual(get_open_advances("PARC test: no such supplier"), [])


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
