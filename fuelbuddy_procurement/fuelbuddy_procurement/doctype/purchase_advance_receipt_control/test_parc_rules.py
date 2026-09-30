# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Checks that need no site: what an advance has left, which receipt row may book against which
advance (oldest first, one per receipt), how refusals are reported, what submit books and closes,
what a receipt cancel gives back or re-opens, the payment cancel guard, the lookup's order, the hooks
wiring and the install field.

Runs from the repository root with plain Python; it installs a stand-in ``frappe`` when the real
one cannot be imported:

    python -m unittest fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.test_parc_rules
"""

import datetime
import importlib
import json
import pathlib
import sys
import types
import unittest
from unittest import mock


def _install_stand_in_frappe():
	"""The few frappe names the modules under test use at import time or in the code tested here."""

	class _dict(dict):  # as frappe.types.frappedict._dict
		__slots__ = ()
		__getattr__ = dict.get
		__setattr__ = dict.__setitem__

	class ValidationError(Exception):
		pass

	class LinkExistsError(ValidationError):
		pass

	class DoesNotExistError(ValidationError):
		pass

	def throw(msg, exc=ValidationError, title=None, **kwargs):
		raise exc(msg)

	def flt(value, precision=None):
		value = float(value or 0)
		return round(value, precision) if precision is not None else value

	modules = {
		name: types.ModuleType(name)
		for name in (
			"frappe",
			"frappe.utils",
			"frappe.model",
			"frappe.model.document",
			"frappe.custom",
			"frappe.custom.doctype",
			"frappe.custom.doctype.custom_field",
			"frappe.custom.doctype.custom_field.custom_field",
		)
	}
	frappe = modules["frappe"]
	frappe._ = lambda text, *args, **kwargs: text
	frappe._dict = _dict
	frappe.ValidationError = ValidationError
	frappe.LinkExistsError = LinkExistsError
	frappe.DoesNotExistError = DoesNotExistError
	frappe.throw = throw
	frappe.clear_last_message = lambda: None
	frappe.whitelist = lambda *args, **kwargs: lambda fn: fn
	frappe.utils = modules["frappe.utils"]
	frappe.utils.flt = flt
	modules["frappe.model.document"].Document = type("Document", (), {})
	modules["frappe.custom.doctype.custom_field.custom_field"].create_custom_fields = lambda *a, **k: None
	sys.modules.update(modules)


try:
	import frappe
except ImportError:
	_install_stand_in_frappe()
	import frappe

from fuelbuddy_procurement import hooks, install
from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control import (
	purchase_advance_receipt_control as parc_module,
)
from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control import (
	CONSUMPTION,
	CONSUMPTIONS,
	EXPECTED,
	PARC,
	PARC_FIELD,
	ParcRefusedError,
	advance_refusal,
	advances_a_receipt_may_name,
	consumed_qty,
	remaining_qty,
)

_dict = frappe._dict
DOCTYPES = pathlib.Path(parc_module.__file__).parent.parent
DAY = datetime.date(2026, 9, 1)


def _advance(name="PARC-1", uses=(), **overrides):
	"""A PARC as the checks see it: an open advance of 400 IG on PO-1. `uses`: its consumption rows as
	(receipt, qty, is_active)."""
	return _dict(
		{
			"name": name,
			"docstatus": 0,
			"purchase_order": "PO-1",
			"purchase_receipt": None,
			"uom_of_item": "IG",
			"qty_of_po": 1000.0,
			EXPECTED: 400.0,
			CONSUMPTIONS: [_dict(purchase_receipt=pr, qty=qty, is_active=active) for pr, qty, active in uses],
			**overrides,
		}
	)


def _document(name="PARC-1", uses=(), **overrides):
	"""An advance whose saves can be watched: `flags`, `append()`, `save()`, `submit()`, `cancel()`."""
	parc = _advance(name, uses, **overrides)
	parc.flags = types.SimpleNamespace()
	parc.append = lambda field, row: parc[field].append(_dict(row))
	parc.save = mock.Mock()
	parc.submit = mock.Mock()
	parc.cancel = mock.Mock()
	return parc


def _row(idx=1, advance="PARC-1", **overrides):
	return _dict(
		{
			"idx": idx,
			"name": f"PRI-{idx}",
			"purchase_order": "PO-1",
			"uom": "IG",
			"qty": 150.0,
			PARC_FIELD: advance,
			**overrides,
		}
	)


def _queued(parc, days=0, created=0, po_status="To Receive and Bill"):
	"""`parc` as the supplier's queue lists it: paid `days` after DAY, entered `created` seconds in."""
	return _dict(
		name=parc.name,
		purchase_order=parc.purchase_order,
		payment_date=DAY + datetime.timedelta(days=days),
		payment_created=datetime.datetime(2026, 9, 1, 8, 0, created),
		uom_of_item=parc.uom_of_item,
		qty_remaining=remaining_qty(parc),
		po_status=po_status,
	)


class TestQuantities(unittest.TestCase):
	def test_what_is_left_is_the_advance_minus_its_active_uses(self):
		parc = _advance(uses=[("PR-1", 100.0, 1), ("PR-2", 50.0, 0), ("PR-3", 30.0, 1)])
		self.assertEqual((consumed_qty(parc), remaining_qty(parc)), (130.0, 270.0))

	def test_the_form_stores_both_on_every_save(self):
		parc = _advance(uses=[("PR-1", 100.0, 1), ("PR-2", 50.0, 0)])
		parc_module.PurchaseAdvanceReceiptControl.validate(parc)
		self.assertEqual((parc.qty_consumed, parc.qty_remaining), (100.0, 300.0))


class TestAdvanceRefusal(unittest.TestCase):
	def refusal(self, parc=None, row=None, advance_supplier="SUP-1"):
		return advance_refusal(parc or _advance(), row or _row(), "SUP-1", advance_supplier)

	def test_a_row_may_book_part_of_an_open_advance(self):
		self.assertIsNone(self.refusal())

	def test_a_row_may_book_all_it_has_left_give_or_take_float_dust(self):
		parc = _advance(uses=[("PR-1", 300.0, 1)])
		self.assertIsNone(self.refusal(parc, _row(qty=100.0)))
		self.assertIsNone(self.refusal(parc, _row(qty=100.009)))

	def test_more_than_it_has_left_names_what_is_left_and_who_used_it_last(self):
		parc = _advance(uses=[("PR-1", 200.0, 1), ("PR-2", 100.0, 1), ("PR-3", 50.0, 0)])
		reason = self.refusal(parc, _row(qty=100.02))
		self.assertIn("has 100.000 IG left; this row books 100.020", reason)
		self.assertIn("without an advance", reason)
		self.assertIn("last used by Purchase Receipt PR-2", reason)

	def test_cancelled_uses_are_back_on_the_advance(self):
		self.assertIsNone(self.refusal(_advance(uses=[("PR-1", 300.0, 0)]), _row(qty=400.0)))

	def test_a_used_up_advance_names_the_receipt_that_used_the_last_of_it(self):
		reason = self.refusal(_advance(docstatus=1, purchase_receipt="PR-7"))
		self.assertIn("used up", reason)
		self.assertIn("PR-7", reason)

	def test_an_advance_closed_by_hand(self):
		self.assertEqual(self.refusal(_advance(docstatus=1)), "is closed")

	def test_a_cancelled_advance(self):
		self.assertIn("cancelled", self.refusal(_advance(docstatus=2)))

	def test_an_advance_to_another_supplier(self):
		reason = self.refusal(advance_supplier="SUP-2")
		self.assertIn("SUP-2", reason)
		self.assertIn("SUP-1", reason)

	def test_a_row_on_another_purchase_order(self):
		reason = self.refusal(row=_row(purchase_order="PO-2"))
		self.assertIn("PO-1", reason)
		self.assertIn("PO-2", reason)
		self.assertIn("no Purchase Order", self.refusal(row=_row(purchase_order=None)))

	def test_a_row_in_another_unit(self):
		reason = self.refusal(row=_row(uom="Litre"))
		self.assertIn("IG", reason)
		self.assertIn("Litre", reason)

	def test_a_row_that_books_nothing(self):
		self.assertIn("books no quantity", self.refusal(row=_row(qty=0)))


class TestReceiptRule(unittest.TestCase):
	def test_a_receipt_may_name_only_the_oldest_advance_with_quantity_left(self):
		oldest, newer, newest = _dict(name="A"), _dict(name="B"), _dict(name="C")
		self.assertEqual(advances_a_receipt_may_name([oldest, newer, newest]), [oldest])
		self.assertEqual(advances_a_receipt_may_name([]), [])


class _ReceiptCase(unittest.TestCase):
	"""A supplier (SUP-1) whose open advances are `self.advances`, queued in `self.queue` order."""

	def setUp(self):
		self.advances = {}
		self.queue = []
		self.locked = {}  # what a locking read returns, where it differs from the plain read
		self.db = mock.Mock()
		self.db.exists.side_effect = lambda doctype, name: doctype == PARC and name in self.advances
		self.db.get_value.side_effect = lambda doctype, name, field: "SUP-1"
		self.get_doc = mock.Mock(side_effect=self._get_doc)
		patches = [
			mock.patch.object(parc_module.frappe, "db", self.db, create=True),
			mock.patch.object(parc_module.frappe, "get_doc", self.get_doc, create=True),
			mock.patch.object(parc_module.frappe, "msgprint", create=True),
			mock.patch.object(parc_module.frappe, "clear_last_message", create=True),
			mock.patch.object(parc_module, "_supplier_advances", lambda supplier, company: self.queue),
		]
		for patch in patches:
			patch.start()
			self.addCleanup(patch.stop)

	def _get_doc(self, doctype, name, for_update=False):
		if for_update and name in self.locked:
			found = self.locked[name]
			if isinstance(found, Exception):
				raise found
			return found
		return self.advances[name]

	def add(self, parc, days=0, created=0, **queued):
		"""`parc` is an open advance of SUP-1, queued as paid `days` after the others' first day."""
		self.advances[parc.name] = parc
		self.queue.append(_queued(parc, days, created, **queued))
		self.queue.sort(key=parc_module._oldest_first)
		return parc

	def receipt(self, *rows, is_return=0, **fields):
		fields = {"supplier": "SUP-1", "company": "CO-1", "name": "PR-9", **fields}
		return _dict(items=list(rows), is_return=is_return, **fields)

	def refused(self, doc, for_update=False):
		with self.assertRaises(ParcRefusedError) as ctx:
			parc_module._named_advances(doc, for_update=for_update)
		return str(ctx.exception)


class TestNamedAdvances(_ReceiptCase):
	"""_named_advances: what a receipt may name, every row at fault listed in row order."""

	def test_rows_naming_nothing_read_nothing(self):
		self.assertEqual(parc_module._named_advances(self.receipt(_row(advance=None))), [])
		self.db.exists.assert_not_called()
		self.get_doc.assert_not_called()

	def test_a_return_cannot_name_an_advance(self):
		self.assertIn("return", self.refused(self.receipt(_row(), is_return=1)))

	def test_an_advance_that_does_not_exist(self):
		self.assertIn("does not exist", self.refused(self.receipt(_row(advance="PARC-NONE"))))

	def test_the_oldest_advance_with_quantity_left_is_used(self):
		parc = self.add(_advance("PARC-1", uses=[("PR-1", 100.0, 1)]))
		self.add(_advance("PARC-2", purchase_order="PO-2"), days=1)
		row = _row(qty=300.0)
		self.assertEqual(parc_module._named_advances(self.receipt(row)), [(parc, row)])

	def test_a_newer_advance_is_refused_while_an_older_one_has_quantity_left(self):
		self.add(_advance("PARC-OLD", purchase_order="PO-0", uses=[("PR-1", 150.0, 1)]))
		self.add(_advance("PARC-1"), days=1)
		message = self.refused(self.receipt(_row()))
		self.assertIn("Row 1: advance PARC-1 is not the oldest advance of SUP-1 with quantity left", message)
		self.assertIn("use PARC-OLD first (250.000 IG left on Purchase Order PO-0, paid 2026-09-01)", message)
		self.assertNotIn("no receipt can be booked", message)

	def test_an_oldest_advance_on_a_closed_or_held_order_stays_first_and_the_refusal_says_why(self):
		for status in ("Closed", "On Hold"):
			with self.subTest(status=status):
				self.advances, self.queue = {}, []
				self.add(_advance("PARC-OLD", purchase_order="PO-0"), po_status=status)
				self.add(_advance("PARC-1"), days=1)
				message = self.refused(self.receipt(_row()))
				self.assertIn("use PARC-OLD first", message)
				self.assertIn(f"Purchase Order PO-0 is {status}, so no receipt can be booked", message)

	def test_payment_date_then_entry_order_then_name_decide_which_is_oldest(self):
		self.add(_advance("PARC-B", purchase_order="PO-2"), days=0, created=5)
		self.add(_advance("PARC-A", purchase_order="PO-3"), days=0, created=5)
		self.add(_advance("PARC-1"), days=0, created=1)
		self.add(_advance("PARC-0", purchase_order="PO-4"), days=1, created=0)
		self.assertEqual([adv.name for adv in self.queue], ["PARC-1", "PARC-A", "PARC-B", "PARC-0"])
		self.assertEqual(len(parc_module._named_advances(self.receipt(_row()))), 1)
		self.assertIn(
			"use PARC-1 first", self.refused(self.receipt(_row(advance="PARC-A", purchase_order="PO-3")))
		)

	def test_older_advances_with_nothing_left_do_not_count(self):
		self.add(_advance("PARC-OLD", purchase_order="PO-0", uses=[("PR-1", 399.995, 1)]))
		self.add(_advance("PARC-1"), days=1)
		self.assertEqual(len(parc_module._named_advances(self.receipt(_row()))), 1)

	def test_a_receipt_uses_one_advance_at_most(self):
		self.add(_advance("PARC-1"))
		self.add(_advance("PARC-2", purchase_order="PO-2"), days=1)
		message = self.refused(
			self.receipt(_row(idx=1), _row(idx=2, advance="PARC-2", purchase_order="PO-2"))
		)
		self.assertEqual(len(message.split("<br>")), 1, message)
		self.assertIn("Row 2: advance PARC-2 cannot be used as well", message)
		self.assertIn("at most 1 advance(s), and row 1 names PARC-1", message)

	def test_every_row_at_fault_is_listed_in_row_order(self):
		self.advances["PARC-USED"] = _advance("PARC-USED", docstatus=1, purchase_receipt="PR-7")
		self.add(_advance("PARC-1"))
		message = self.refused(
			self.receipt(
				_row(idx=1, advance="PARC-USED"),
				_row(idx=2, qty=500.0),
				_row(idx=3),
				_row(idx=4, advance="PARC-NONE"),
			)
		)
		lines = message.split("<br>")
		self.assertEqual(len(lines), 4, message)
		self.assertTrue(lines[0].startswith("Row 1: advance PARC-USED is used up") and "PR-7" in lines[0])
		self.assertTrue(lines[1].startswith("Row 2: advance PARC-1 has 400.000 IG left"), message)
		self.assertTrue(lines[2].startswith("Row 3: advance PARC-1 is named on more than one row"), message)
		self.assertTrue(lines[3].startswith("Row 4: advance PARC-NONE does not exist"), message)


class TestSubmitRecheck(_ReceiptCase):
	"""On submit, _named_advances decides on locking reads, taken oldest first."""

	def test_submit_locks_the_queue_ahead_of_the_named_advance_oldest_first(self):
		self.add(_advance("PARC-Z", purchase_order="PO-0", uses=[("PR-1", 300.0, 1)]))
		self.add(_advance("PARC-1"), days=1)
		self.add(_advance("PARC-2", purchase_order="PO-2"), days=2)
		# By the time of the submit another receipt has used up PARC-Z.
		self.locked["PARC-Z"] = _advance("PARC-Z", docstatus=1, purchase_receipt="PR-2")
		named = parc_module._named_advances(self.receipt(_row()), for_update=True)
		self.assertEqual([(parc.name, row.idx) for parc, row in named], [("PARC-1", 1)])
		self.assertEqual(
			self.get_doc.call_args_list,
			[mock.call(PARC, "PARC-Z", for_update=True), mock.call(PARC, "PARC-1", for_update=True)],
		)

	def test_submit_refuses_what_its_locked_read_shows_used_since_the_save(self):
		"""The save check saw 400 left; by submit another receipt has booked 300. The submit goes by
		its own locked read: it refuses the receipt and names the receipt that used the advance."""
		self.add(_advance("PARC-1"))
		self.locked["PARC-1"] = _document("PARC-1", uses=[("PR-FIRST", 300.0, 1)])
		doc = self.receipt(_row(qty=300.0))
		parc_module.check_named_parcs_on_purchase_receipt(doc)
		with self.assertRaises(ParcRefusedError) as ctx:
			parc_module.consume_named_parcs_on_purchase_receipt(doc)
		self.assertIn("has 100.000 IG left", str(ctx.exception))
		self.assertIn("last used by Purchase Receipt PR-FIRST", str(ctx.exception))
		locked = self.locked["PARC-1"]
		self.assertEqual(len(locked[CONSUMPTIONS]), 1)
		locked.save.assert_not_called()
		locked.submit.assert_not_called()

	def test_submit_refuses_an_advance_its_locked_read_shows_used_up(self):
		self.add(_advance("PARC-1"))
		self.locked["PARC-1"] = _advance("PARC-1", docstatus=1, purchase_receipt="PR-FIRST")
		message = self.refused(self.receipt(_row()), for_update=True)
		self.assertIn("used up: Purchase Receipt PR-FIRST used the last of it", message)

	def test_an_advance_deleted_since_the_save_reads_as_missing(self):
		self.add(_advance("PARC-1"))
		self.locked["PARC-1"] = frappe.DoesNotExistError("gone")
		self.assertIn("does not exist", self.refused(self.receipt(_row()), for_update=True))


class TestConsume(_ReceiptCase):
	"""What the submit books, and when it closes the advance."""

	def setUp(self):
		super().setUp()
		patch = mock.patch.object(parc_module, "_received_qty", return_value=407.0)
		patch.start()
		self.addCleanup(patch.stop)

	def submit(self, parc, row, **fields):
		self.add(parc)
		doc = self.receipt(row, posting_date=DAY, grand_total=4070.0, **fields)
		parc_module.consume_named_parcs_on_purchase_receipt(doc)
		return parc

	def test_a_receipt_books_part_of_an_advance_and_leaves_it_open(self):
		parc = self.submit(_document(uses=[("PR-1", 100.0, 1)]), _row(qty=150.0))
		self.assertEqual(
			parc[CONSUMPTIONS][-1],
			{
				"purchase_receipt": "PR-9",
				"purchase_receipt_item": "PRI-1",
				"posting_date": DAY,
				"qty": 150.0,
				"is_active": 1,
			},
		)
		self.assertEqual(remaining_qty(parc), 150.0)
		parc.save.assert_called_once_with()
		parc.submit.assert_not_called()
		self.assertTrue(parc.flags.ignore_permissions)
		self.assertIsNone(parc.purchase_receipt)

	def test_the_receipt_that_uses_the_last_of_an_advance_closes_it(self):
		parc = self.submit(_document(uses=[("PR-1", 300.0, 1), ("PR-2", 50.0, 0)]), _row(qty=100.0))
		parc.submit.assert_called_once_with()
		parc.save.assert_not_called()
		self.assertEqual(
			(
				parc.purchase_receipt,
				parc.qty_of_pr,
				parc.grand_total_of_pr,
				parc.qty_left_to_be_received_from_po,
			),
			("PR-9", 400.0, 4070.0, 593.0),
		)

	def test_float_dust_left_over_closes_it_too(self):
		parc = self.submit(_document(uses=[("PR-1", 300.0, 1)]), _row(qty=99.995))
		parc.submit.assert_called_once_with()


class TestGiveBack(unittest.TestCase):
	"""give_back_parcs_on_purchase_receipt_cancel: rows made inactive, closed advances re-opened."""

	def setUp(self):
		self.parcs, self.using, self.closed, self.copies = {}, [], [], []
		self.get_all = mock.Mock(
			side_effect=lambda doctype, filters, pluck: self.using if doctype == CONSUMPTION else self.closed
		)
		patches = [
			mock.patch.object(parc_module.frappe, "get_all", self.get_all, create=True),
			mock.patch.object(
				parc_module.frappe,
				"get_doc",
				lambda doctype, name, for_update=False: self.parcs[name],
				create=True,
			),
			mock.patch.object(parc_module.frappe, "copy_doc", self._copy, create=True),
			mock.patch.object(parc_module, "_received_qty", return_value=500.0),
		]
		for patch in patches:
			patch.start()
			self.addCleanup(patch.stop)

	def _copy(self, parc):
		copy = _dict(json.loads(json.dumps(parc, default=lambda value: None)))
		copy[CONSUMPTIONS] = [_dict(row) for row in copy[CONSUMPTIONS]]
		copy.insert = mock.Mock()
		self.copies.append(copy)
		return copy

	def cancel(self, receipt="PR-9"):
		parc_module.give_back_parcs_on_purchase_receipt_cancel(_dict(name=receipt))
		self.assertEqual(self.get_all.call_args_list[0].kwargs["filters"]["purchase_receipt"], receipt)

	def test_cancel_gives_quantity_back_to_an_open_advance(self):
		parc = self.parcs["PARC-1"] = _document(uses=[("PR-8", 100.0, 1), ("PR-9", 150.0, 1)])
		self.using = ["PARC-1"]
		self.cancel()
		self.assertEqual([row.is_active for row in parc[CONSUMPTIONS]], [1, 0])
		self.assertEqual(remaining_qty(parc), 300.0)
		parc.save.assert_called_once_with()
		parc.cancel.assert_not_called()

	def test_cancel_reopens_a_closed_advance_as_a_fresh_draft(self):
		parc = self.parcs["PARC-1"] = _document(
			uses=[("PR-8", 300.0, 1), ("PR-9", 100.0, 1)],
			docstatus=1,
			purchase_receipt="PR-9",
			qty_of_pr=400.0,
			grand_total_of_pr=1000.0,
		)
		self.using, self.closed = ["PARC-1"], ["PARC-1"]
		self.cancel()
		parc.cancel.assert_called_once_with()
		self.assertEqual(parc.ignore_linked_doctypes, ["Purchase Receipt"])
		self.assertEqual([row.is_active for row in parc[CONSUMPTIONS]], [1, 1])  # history as closed
		(draft,) = self.copies
		draft.insert.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(
			(draft.docstatus, draft.purchase_receipt, draft.qty_of_pr, draft.grand_total_of_pr),
			(0, None, None, None),
		)
		self.assertEqual(
			[(row.purchase_receipt, row.is_active) for row in draft[CONSUMPTIONS]], [("PR-8", 1), ("PR-9", 0)]
		)
		self.assertEqual(remaining_qty(draft), 100.0)
		self.assertEqual(draft.qty_left_to_be_received_from_po, 1000.0 - 400.0 - 500.0)

	def test_cancel_reopens_in_full_an_advance_closed_whole_before_consumption_rows(self):
		self.parcs["PARC-1"] = _document(docstatus=1, purchase_receipt="PR-9")
		self.closed = ["PARC-1"]
		self.cancel()
		(draft,) = self.copies
		self.assertEqual((draft[CONSUMPTIONS], remaining_qty(draft)), ([], 400.0))

	def test_cancelled_parcs_keep_their_rows_as_history(self):
		parc = self.parcs["PARC-1"] = _document(uses=[("PR-9", 100.0, 1)], docstatus=2)
		self.using = ["PARC-1"]
		self.cancel()
		parc.save.assert_not_called()
		parc.cancel.assert_not_called()
		self.assertEqual(self.copies, [])


class TestPaymentCancel(unittest.TestCase):
	"""delete_draft_parcs_on_payment_entry_cancel: in-use advances block the cancel."""

	def setUp(self):
		self.parc = _document()
		self.naming = []
		self.delete_doc = mock.Mock()
		get_all = mock.Mock(
			side_effect=lambda doctype, filters, pluck: ["PARC-1"] if doctype == PARC else self.naming
		)
		patches = [
			mock.patch.object(parc_module.frappe, "get_all", get_all, create=True),
			mock.patch.object(
				parc_module.frappe, "get_doc", lambda doctype, name, for_update=False: self.parc, create=True
			),
			mock.patch.object(parc_module.frappe, "delete_doc", self.delete_doc, create=True),
		]
		for patch in patches:
			patch.start()
			self.addCleanup(patch.stop)

	def cancel_payment(self):
		parc_module.delete_draft_parcs_on_payment_entry_cancel(_dict(name="PE-1"))

	def test_an_unused_advance_is_deleted(self):
		self.cancel_payment()
		self.delete_doc.assert_called_once_with(PARC, "PARC-1", ignore_permissions=True, force=True)

	def test_an_advance_a_receipt_has_used_part_of_blocks_the_cancel(self):
		self.parc[CONSUMPTIONS].append(_dict(purchase_receipt="PR-1", qty=100.0, is_active=1))
		with self.assertRaises(frappe.LinkExistsError) as ctx:
			self.cancel_payment()
		self.assertIn("PARC-1 is in use by Purchase Receipt PR-1", str(ctx.exception))
		self.delete_doc.assert_not_called()

	def test_an_advance_a_draft_receipt_names_blocks_the_cancel(self):
		self.naming = ["PR-DRAFT"]
		with self.assertRaises(frappe.LinkExistsError):
			self.cancel_payment()
		self.delete_doc.assert_not_called()

	def test_receipts_cancelled_since_do_not_block_it(self):
		self.parc[CONSUMPTIONS].append(_dict(purchase_receipt="PR-1", qty=100.0, is_active=0))
		self.cancel_payment()
		self.delete_doc.assert_called_once()


class TestOpenAdvancesLookup(unittest.TestCase):
	def setUp(self):
		def row(name, days, created, expected=400.0):
			return _dict(
				name=name,
				payment_date=DAY + datetime.timedelta(days=days),
				payment_created=datetime.datetime(2026, 9, 1, 8, 0, created),
				uom_of_item="IG",
				**{EXPECTED: expected},
			)

		drafts = [
			row("PARC-C", 2, 0),
			row("PARC-B2", 0, 5),
			row("PARC-A", 0, 9),
			row("PARC-B1", 0, 5),
			row("PARC-E", 1, 0),
		]
		used = {"PARC-A": 100.0, "PARC-E": 399.995}
		patches = [
			mock.patch.object(parc_module, "_draft_advances", lambda supplier, company=None: drafts),
			mock.patch.object(parc_module, "_consumed_by", lambda names: used),
			mock.patch.object(parc_module.frappe, "has_permission", create=True),
		]
		for patch in patches:
			patch.start()
			self.addCleanup(patch.stop)

	def test_open_advances_come_oldest_first_with_what_each_has_left(self):
		rows = parc_module.get_open_advances("SUP-1")
		# Same payment (date and entry) goes by name; PARC-E has nothing left.
		self.assertEqual([r.name for r in rows], ["PARC-B1", "PARC-B2", "PARC-A", "PARC-C"])
		self.assertEqual([r.qty_remaining for r in rows], [400.0, 400.0, 300.0, 400.0])
		self.assertEqual(rows[2].qty_consumed, 100.0)
		self.assertNotIn("payment_created", rows[0])
		parc_module.frappe.has_permission.assert_called_once_with(PARC, "read", throw=True)


class TestWiring(unittest.TestCase):
	def test_hooks_point_at_real_handlers(self):
		paths = [hooks.after_install, hooks.after_migrate]
		paths += [path for events in hooks.doc_events.values() for path in events.values()]
		for path in paths:
			module, _dot, attr = path.rpartition(".")
			self.assertTrue(callable(getattr(importlib.import_module(module), attr, None)), path)

	def test_receipt_events_check_on_save_book_on_submit_and_give_back_on_cancel(self):
		events = hooks.doc_events["Purchase Receipt"]
		self.assertEqual(set(events), {"validate", "on_submit", "on_cancel"})
		self.assertTrue(events["on_submit"].endswith(".consume_named_parcs_on_purchase_receipt"))
		self.assertTrue(events["on_cancel"].endswith(".give_back_parcs_on_purchase_receipt_cancel"))

	def test_install_field_links_a_receipt_row_to_one_advance(self):
		(field,) = install.CUSTOM_FIELDS["Purchase Receipt Item"]
		self.assertEqual(
			(field["fieldname"], field["fieldtype"], field["options"]), (PARC_FIELD, "Link", PARC)
		)
		self.assertEqual(field["no_copy"], 1)  # duplicates and returns start without an advance
		self.assertIn([PARC, "docstatus", "=", 0], json.loads(field["link_filters"]))

	def test_the_advance_carries_its_consumption_rows(self):
		parc = json.loads(
			(
				DOCTYPES / "purchase_advance_receipt_control" / "purchase_advance_receipt_control.json"
			).read_text()
		)
		fields = {field["fieldname"]: field for field in parc["fields"]}
		self.assertEqual(
			(fields[CONSUMPTIONS]["fieldtype"], fields[CONSUMPTIONS]["options"]), ("Table", CONSUMPTION)
		)
		self.assertEqual(fields["qty_remaining"]["in_list_view"], 1)
		self.assertTrue(set(parc["field_order"]) >= {CONSUMPTIONS, "qty_consumed", "qty_remaining"})
		child = json.loads(
			(DOCTYPES / "purchase_advance_consumption" / "purchase_advance_consumption.json").read_text()
		)
		columns = {field["fieldname"]: field for field in child["fields"]}
		self.assertEqual((child["name"], child["istable"]), (CONSUMPTION, 1))
		# Not a Link: a link to a cancelled receipt would block every later save of the advance.
		self.assertEqual(columns["purchase_receipt"]["fieldtype"], "Data")
		self.assertEqual(columns["is_active"]["default"], "1")

	def test_migrate_fills_the_quantities_only_where_they_differ(self):
		stale = _document("PARC-OLD", qty_consumed=0.0, qty_remaining=0.0)
		stale.db_set = mock.Mock()
		current = _document("PARC-NEW", uses=[("PR-1", 100.0, 1)], qty_consumed=100.0, qty_remaining=300.0)
		current.db_set = mock.Mock()
		parcs = {"PARC-OLD": stale, "PARC-NEW": current}
		with (
			mock.patch.object(
				install.frappe, "get_all", lambda doctype, filters, pluck: list(parcs), create=True
			),
			mock.patch.object(install.frappe, "get_doc", lambda doctype, name: parcs[name], create=True),
		):
			install.fill_advance_quantities()
		stale.db_set.assert_called_once_with(
			{"qty_consumed": 0.0, "qty_remaining": 400.0}, update_modified=False
		)
		current.db_set.assert_not_called()


if __name__ == "__main__":
	unittest.main()
