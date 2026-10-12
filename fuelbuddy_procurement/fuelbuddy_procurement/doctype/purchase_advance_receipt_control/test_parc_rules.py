# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Checks that need no site: what an advance has left, which desk receipt row may book against which
advance (any open advance of the supplier, in any order, one row each; never more than it has
available net of receipt split holds; not while its order is Closed or On Hold), how refusals are
reported, what submit books and closes, what a receipt cancel gives back or re-opens, the payment
cancel guard (receipts and holds), the lookups' order and fields, the hooks wiring and the install
fields.

Runs from the repository root with plain Python; it installs a stand-in ``frappe`` when the real
one cannot be imported (``_install_stand_in_frappe``, which the receipt split hold rules reuse):

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
	"""The frappe names the modules under test use at import time, or that their tests patch in."""

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

	class PermissionError(Exception):
		pass

	class TimestampMismatchError(ValidationError):
		pass

	class UniqueValidationError(ValidationError):
		pass

	class DuplicateEntryError(NameError):
		pass

	class QueryDeadlockError(Exception):
		pass

	class QueryTimeoutError(Exception):
		pass

	def throw(msg, exc=ValidationError, title=None, **kwargs):
		raise exc(msg)

	def flt(value, precision=None):
		value = float(value or 0)
		return round(value, precision) if precision is not None else value

	def cint(value):
		try:
			return int(float(value or 0))
		except (TypeError, ValueError):
			return 0

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
	frappe.PermissionError = PermissionError
	frappe.TimestampMismatchError = TimestampMismatchError
	frappe.UniqueValidationError = UniqueValidationError
	frappe.DuplicateEntryError = DuplicateEntryError
	frappe.QueryDeadlockError = QueryDeadlockError
	frappe.QueryTimeoutError = QueryTimeoutError
	frappe.throw = throw
	frappe.clear_last_message = lambda: None
	frappe.clear_messages = lambda: None
	frappe.whitelist = lambda *args, **kwargs: lambda fn: fn
	frappe.conf = _dict()
	frappe.utils = modules["frappe.utils"]
	frappe.utils.flt = flt
	frappe.utils.cint = cint
	frappe.utils.strip_html = lambda text: text
	frappe.utils.escape_html = lambda text: text
	frappe.utils.now_datetime = lambda: datetime.datetime(2026, 10, 8, 9, 0, 0)
	frappe.utils.nowdate = lambda: "2026-10-08"
	frappe.utils.getdate = lambda value=None: datetime.date.fromisoformat(str(value)[:10])
	modules["frappe.model.document"].Document = type("Document", (), {})
	modules["frappe.custom.doctype.custom_field.custom_field"].create_custom_fields = lambda *a, **k: None
	sys.modules.update(modules)


try:
	import frappe
except ImportError:
	_install_stand_in_frappe()
	import frappe

from fuelbuddy_procurement import allocation, hooks, install, receipt_events, receipt_hold
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
	consumed_qty,
	remaining_qty,
	skipped_in_queue,
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


def _holder(hold="REQ-7-v1", qty=100.0, request_id="REQ-7", version=1):
	return _dict(hold=hold, request_id=request_id, version=version, qty=qty, qty_litres=round(qty * 4.54609, 3))


class TestQuantities(unittest.TestCase):
	def test_what_is_left_is_the_advance_minus_its_active_uses(self):
		parc = _advance(uses=[("PR-1", 100.0, 1), ("PR-2", 50.0, 0), ("PR-3", 30.0, 1)])
		self.assertEqual((consumed_qty(parc), remaining_qty(parc)), (130.0, 270.0))

	def test_the_form_stores_both_on_every_save(self):
		parc = _advance(uses=[("PR-1", 100.0, 1), ("PR-2", 50.0, 0)])
		parc_module.PurchaseAdvanceReceiptControl.validate(parc)
		self.assertEqual((parc.qty_consumed, parc.qty_remaining), (100.0, 300.0))


class TestAdvanceRefusal(unittest.TestCase):
	def refusal(self, parc=None, row=None, advance_supplier="SUP-1", holders=None):
		return advance_refusal(parc or _advance(), row or _row(), "SUP-1", advance_supplier, holders)

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

	def test_held_quantity_is_not_available_to_a_desk_row(self):
		holders = [_holder("REQ-7-v2", 300.0, "REQ-7", 2)]
		self.assertIsNone(self.refusal(row=_row(qty=100.0), holders=holders))
		reason = self.refusal(row=_row(qty=100.5), holders=holders)
		self.assertIn("has 400.000 IG left, 300.000 of it held for receipt split holds (REQ-7 v2)", reason)
		self.assertIn("this row books 100.500", reason)

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


class TestSkipped(unittest.TestCase):
	def test_only_advances_on_closed_or_held_orders_are_skipped(self):
		for status in ("Closed", "On Hold"):
			self.assertTrue(skipped_in_queue(_dict(po_status=status)), status)
		for status in ("To Receive and Bill", "To Receive", "To Bill", "Completed", "Delivered", None):
			self.assertFalse(skipped_in_queue(_dict(po_status=status)), status)

	def test_the_oldest_first_rule_is_retired(self):
		self.assertFalse(hasattr(parc_module, "advances_a_receipt_may_name"))


def _msgprint(msg, *args, raise_exception=False, **kwargs):
	"""As frappe.msgprint without the page message. It still raises when asked to, because the real
	frappe.throw raises through msgprint."""
	if not raise_exception:
		return
	if isinstance(raise_exception, type) and issubclass(raise_exception, Exception):
		raise raise_exception(msg)
	if isinstance(raise_exception, Exception):
		raise raise_exception
	raise parc_module.frappe.ValidationError(msg)


class _ReceiptCase(unittest.TestCase):
	"""A supplier (SUP-1, company CO-1) whose advances are `self.advances`, paid in `self.paid` order;
	`self.holders` is what live receipt split holds hold on each advance."""

	def setUp(self):
		self.advances = {}
		self.paid = []  # advance names, oldest payment first
		self.status = {}  # Purchase Order -> status, when not To Receive and Bill
		self.company = {}  # Purchase Order -> company, when not CO-1
		self.holders = {}
		self.locked = {}  # what a locking read returns, where it differs from the plain read
		self.db = mock.Mock()
		self.db.exists.side_effect = lambda doctype, name: doctype == PARC and name in self.advances
		self.db.get_value.side_effect = self._order
		self.get_doc = mock.Mock(side_effect=self._get_doc)
		self.holders_by_parc = mock.Mock(side_effect=lambda names, for_update=False: dict(self.holders))
		patches = [
			mock.patch.object(parc_module.frappe, "db", self.db, create=True),
			mock.patch.object(parc_module.frappe, "get_doc", self.get_doc, create=True),
			mock.patch.object(parc_module.frappe, "msgprint", _msgprint, create=True),
			mock.patch.object(parc_module.frappe, "clear_last_message", create=True),
			mock.patch.object(allocation, "queue_order", self._queue_order),
			mock.patch.object(allocation, "holders_by_parc", self.holders_by_parc),
		]
		for patch in patches:
			patch.start()
			self.addCleanup(patch.stop)

	def _order(self, doctype, name, fields=None, as_dict=False):
		self.assertEqual(doctype, "Purchase Order")
		return _dict(
			name=name,
			supplier="SUP-1",
			company=self.company.get(name, "CO-1"),
			status=self.status.get(name, "To Receive and Bill"),
		)

	def _queue_order(self, names):
		return [name for name in self.paid if name in names] + sorted(n for n in names if n not in self.paid)

	def _get_doc(self, doctype, name, for_update=False):
		if for_update and name in self.locked:
			found = self.locked[name]
			if isinstance(found, Exception):
				raise found
			return found
		return self.advances[name]

	def add(self, parc):
		"""`parc` is an open advance of SUP-1, paid after the ones added before it."""
		self.advances[parc.name] = parc
		self.paid.append(parc.name)
		return parc

	def receipt(self, *rows, is_return=0, **fields):
		fields = {"supplier": "SUP-1", "company": "CO-1", "name": "PR-9", **fields}
		return _dict(items=list(rows), is_return=is_return, **fields)

	def refused(self, doc, for_update=False):
		with self.assertRaises(ParcRefusedError) as ctx:
			parc_module._named_advances(doc, for_update=for_update)
		return str(ctx.exception)


class TestNamedAdvances(_ReceiptCase):
	"""_named_advances: what a desk receipt may name, every row at fault listed in row order."""

	def test_rows_naming_nothing_read_nothing(self):
		self.assertEqual(parc_module._named_advances(self.receipt(_row(advance=None))), [])
		self.db.exists.assert_not_called()
		self.get_doc.assert_not_called()

	def test_a_return_cannot_name_an_advance(self):
		self.assertIn("return", self.refused(self.receipt(_row(), is_return=1)))

	def test_an_advance_that_does_not_exist(self):
		self.assertIn("does not exist", self.refused(self.receipt(_row(advance="PARC-NONE"))))

	def test_a_newer_advance_may_be_used_while_an_older_one_has_quantity_left(self):
		self.add(_advance("PARC-OLD", purchase_order="PO-0", uses=[("PR-1", 150.0, 1)]))
		newer = self.add(_advance("PARC-1"))
		row = _row()
		self.assertEqual(parc_module._named_advances(self.receipt(row)), [(newer, row)])

	def test_a_receipt_may_name_several_advances_in_any_order_one_row_each(self):
		first = self.add(_advance("PARC-1"))
		second = self.add(_advance("PARC-2", purchase_order="PO-2"))
		rows = [_row(idx=1, advance="PARC-2", purchase_order="PO-2"), _row(idx=2, advance="PARC-1")]
		named = parc_module._named_advances(self.receipt(*rows))
		self.assertEqual([(parc.name, row.idx) for parc, row in named], [("PARC-2", 1), ("PARC-1", 2)])
		self.assertEqual([parc for parc, _row_ in named], [second, first])

	def test_an_advance_on_a_closed_or_held_order_is_refused_and_the_refusal_says_why(self):
		for status in ("Closed", "On Hold"):
			with self.subTest(status=status):
				self.advances, self.paid = {}, []
				self.add(_advance("PARC-OLD", purchase_order="PO-0"))
				self.status["PO-0"] = status
				message = self.refused(self.receipt(_row(advance="PARC-OLD", purchase_order="PO-0")))
				self.assertEqual(
					message,
					f"Row 1: advance PARC-OLD is skipped while its Purchase Order PO-0 is {status}: no "
					"receipt can use it until the order is re-opened",
				)

	def test_a_reopened_order_makes_its_advance_usable_again(self):
		"""The skip is read from the order's status at each check: nothing is stored on the advance."""
		old = self.add(_advance("PARC-OLD", purchase_order="PO-0"))
		self.status["PO-0"] = "Closed"
		row = _row(advance="PARC-OLD", purchase_order="PO-0")
		self.assertIn("is skipped", self.refused(self.receipt(row)))
		self.status["PO-0"] = "To Receive and Bill"
		self.assertEqual(parc_module._named_advances(self.receipt(row)), [(old, row)])
		self.assertNotIn("skipped", old)

	def test_an_advance_of_another_company_is_refused(self):
		self.add(_advance("PARC-1"))
		self.company["PO-1"] = "CO-2"
		self.assertIn("is an advance of company CO-2; this receipt is for CO-1", self.refused(self.receipt(_row())))

	def test_held_quantity_cannot_be_named(self):
		self.add(_advance("PARC-1"))
		self.holders["PARC-1"] = [_holder("REQ-7-v1", 300.0)]
		message = self.refused(self.receipt(_row(qty=150.0)))
		self.assertIn("Row 1: advance PARC-1 has 400.000 IG left, 300.000 of it held", message)
		self.assertIn("(REQ-7 v1)", message)
		self.assertEqual(len(parc_module._named_advances(self.receipt(_row(qty=100.0)))), 1)

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
	"""On submit, _named_advances decides on locking reads: the named advances oldest first, then the
	holds on them."""

	def test_submit_locks_the_named_advances_oldest_first_then_reads_the_holds_locked(self):
		self.add(_advance("PARC-Z", purchase_order="PO-0"))
		self.add(_advance("PARC-1"))
		self.add(_advance("PARC-2", purchase_order="PO-2"))
		doc = self.receipt(_row(idx=1, advance="PARC-2", purchase_order="PO-2"), _row(idx=2, advance="PARC-1"))
		named = parc_module._named_advances(doc, for_update=True)
		self.assertEqual([(parc.name, row.idx) for parc, row in named], [("PARC-2", 1), ("PARC-1", 2)])
		self.assertEqual(
			self.get_doc.call_args_list,
			[mock.call(PARC, "PARC-1", for_update=True), mock.call(PARC, "PARC-2", for_update=True)],
		)
		self.holders_by_parc.assert_called_once_with(["PARC-1", "PARC-2"], for_update=True)

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

	def test_submit_refuses_quantity_a_hold_took_since_the_save(self):
		self.add(_advance("PARC-1"))
		doc = self.receipt(_row(qty=300.0))
		parc_module.check_named_parcs_on_purchase_receipt(doc)
		self.holders["PARC-1"] = [_holder("REQ-8-v3", 200.0, "REQ-8", 3)]
		self.assertIn("200.000 of it held for receipt split holds (REQ-8 v3)", self.refused(doc, for_update=True))

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
				"receipt_split_hold": None,
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

	def test_a_booking_for_a_hold_carries_the_hold(self):
		parc = _document(uses=[("PR-1", 100.0, 1)])
		parc_module.book_consumption(parc, self.receipt(posting_date=DAY), _row(qty=50.0), hold="REQ-7-v2")
		self.assertEqual(parc[CONSUMPTIONS][-1]["receipt_split_hold"], "REQ-7-v2")


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
	"""delete_draft_parcs_on_payment_entry_cancel: in-use and held advances block the cancel."""

	def setUp(self):
		self.parcs = {"PARC-1": _document("PARC-1"), "PARC-2": _document("PARC-2", purchase_order="PO-2")}
		self.naming = []
		self.holders = {}
		self.delete_doc = mock.Mock()
		self.get_doc = mock.Mock(side_effect=lambda doctype, name, for_update=False: self.parcs[name])
		get_all = mock.Mock(
			side_effect=lambda doctype, filters, pluck, **kwargs: list(self.parcs) if doctype == PARC else self.naming
		)
		self.holders_by_parc = mock.Mock(side_effect=lambda names, for_update=False: self.holders)
		patches = [
			mock.patch.object(parc_module.frappe, "get_all", get_all, create=True),
			mock.patch.object(parc_module.frappe, "get_doc", self.get_doc, create=True),
			mock.patch.object(parc_module.frappe, "delete_doc", self.delete_doc, create=True),
			mock.patch.object(allocation, "holders_by_parc", self.holders_by_parc),
		]
		for patch in patches:
			patch.start()
			self.addCleanup(patch.stop)

	def cancel_payment(self):
		parc_module.delete_draft_parcs_on_payment_entry_cancel(_dict(name="PE-1"))

	def test_unused_advances_are_deleted_after_every_one_was_locked_and_the_holds_read(self):
		self.cancel_payment()
		self.assertEqual(
			self.get_doc.call_args_list,
			[mock.call(PARC, "PARC-1", for_update=True), mock.call(PARC, "PARC-2", for_update=True)],
		)
		self.holders_by_parc.assert_called_once_with(["PARC-1", "PARC-2"], for_update=True)
		self.assertEqual(
			self.delete_doc.call_args_list,
			[
				mock.call(PARC, "PARC-1", ignore_permissions=True, force=True),
				mock.call(PARC, "PARC-2", ignore_permissions=True, force=True),
			],
		)

	def test_an_advance_a_receipt_has_used_part_of_blocks_the_cancel(self):
		self.parcs["PARC-1"][CONSUMPTIONS].append(_dict(purchase_receipt="PR-1", qty=100.0, is_active=1))
		with self.assertRaises(frappe.LinkExistsError) as ctx:
			self.cancel_payment()
		self.assertIn("PARC-1 is in use by Purchase Receipt PR-1", str(ctx.exception))
		self.delete_doc.assert_not_called()

	def test_an_advance_a_draft_receipt_names_blocks_the_cancel(self):
		self.naming = ["PR-DRAFT"]
		with self.assertRaises(frappe.LinkExistsError):
			self.cancel_payment()
		self.delete_doc.assert_not_called()

	def test_an_advance_a_live_hold_holds_quantity_on_blocks_the_cancel(self):
		self.holders["PARC-2"] = [_holder("REQ-7-v2", 50.0, "REQ-7", 2)]
		with self.assertRaises(frappe.LinkExistsError) as ctx:
			self.cancel_payment()
		self.assertIn("Advance PARC-2 is held for receipt split hold REQ-7-v2 (request REQ-7 v2)", str(ctx.exception))
		self.delete_doc.assert_not_called()

	def test_receipts_cancelled_since_do_not_block_it(self):
		self.parcs["PARC-1"][CONSUMPTIONS].append(_dict(purchase_receipt="PR-1", qty=100.0, is_active=0))
		self.cancel_payment()
		self.assertEqual(self.delete_doc.call_count, 2)


def _sources(advances, orders, lines, queue):
	return _dict(
		scope=_dict(supplier="SUP-1", company=None, item_code=None),
		orders={order.name: order for order in orders},
		lines={line.name: line for line in lines},
		advances={adv.name: adv for adv in advances},
		queue=queue,
	)


class TestLookups(unittest.TestCase):
	"""get_open_advances and get_open_purchase_orders, on the view allocation builds."""

	def setUp(self):
		def order(name, status="To Receive and Bill", date="2026-09-01", created=0):
			return _dict(
				name=name,
				supplier="SUP-1",
				company="CO-1",
				status=status,
				docstatus=1,
				transaction_date=date,
				schedule_date=date,
				creation=f"2026-09-01 08:00:{created:02d}",
				setup_key=f"setup-{name}",
			)

		def line(name, po, received=0.0, qty=1000.0, idx=1):
			return _dict(
				name=name,
				purchase_order=po,
				idx=idx,
				item_code="FUEL",
				uom="IG",
				stock_uom="Litre",
				conversion_factor=4.54609,
				qty=qty,
				received_qty=received,
			)

		def adv(name, po, days, created, expected=400.0, consumed=0.0):
			return _dict(
				name=name,
				purchase_order=po,
				payment_entry=f"PE-{name}",
				payment_date=DAY + datetime.timedelta(days=days),
				payment_created=datetime.datetime(2026, 9, 1, 8, 0, created),
				payment_docstatus=1,
				docstatus=0,
				purchase_receipt=None,
				uom_of_item="IG",
				advance_paid=1000.0,
				expected=expected,
				consumed=consumed,
			)

		advances = [
			adv("PARC-C", "PO-HELD", 2, 0),
			adv("PARC-B2", "PO-1", 0, 5),
			adv("PARC-A", "PO-CLOSED", 0, 9, consumed=100.0),
			adv("PARC-B1", "PO-1", 0, 5),
			adv("PARC-E", "PO-2", 1, 0, consumed=399.995),
			adv("PARC-S", "PO-DONE", 3, 0),
		]
		orders = [
			order("PO-1", date="2026-09-03"),
			order("PO-2", date="2026-09-02"),
			order("PO-HELD", status="On Hold"),
			order("PO-CLOSED", status="Closed"),
			order("PO-DONE", status="To Bill", date="2026-08-30"),
		]
		lines = [
			line("POI-1", "PO-1", received=100.0),
			line("POI-2", "PO-2"),
			line("POI-HELD", "PO-HELD"),
			line("POI-CLOSED", "PO-CLOSED"),
			line("POI-DONE", "PO-DONE", received=1000.0),
		]
		queue = [a.name for a in sorted(advances, key=allocation.queue_key)]
		held = [
			_dict(
				name="L-1", hold="REQ-7-v1", request_id="REQ-7", version=1, source_type="PARC", parc="PARC-B1",
				purchase_order_item="POI-1", qty=150.0, qty_litres=681.914,
			),
			_dict(
				name="L-2", hold="REQ-7-v1", request_id="REQ-7", version=1, source_type="PO", parc=None,
				purchase_order_item="POI-1", qty=50.0, qty_litres=227.305,
			),
		]
		self.view = allocation.build_view(_sources(advances, orders, lines, queue), held)
		patches = [
			mock.patch.object(allocation, "snapshot", mock.Mock(return_value=self.view)),
			mock.patch.object(parc_module.frappe, "has_permission", create=True),
		]
		for patch in patches:
			patch.start()
			self.addCleanup(patch.stop)

	def test_open_advances_come_oldest_first_with_what_each_has_left(self):
		rows = parc_module.get_open_advances("SUP-1")
		# Same payment (date and entry) goes by name; PARC-E has nothing left.
		self.assertEqual([r.name for r in rows], ["PARC-B1", "PARC-B2", "PARC-A", "PARC-C", "PARC-S"])
		self.assertEqual([r.qty_remaining for r in rows], [400.0, 400.0, 300.0, 400.0, 400.0])
		self.assertEqual(rows[2].qty_consumed, 100.0)
		self.assertNotIn("payment_created", rows[0])
		parc_module.frappe.has_permission.assert_called_once_with(PARC, "read", throw=True)
		allocation.snapshot.assert_called_once_with("SUP-1", None, None, for_request=None)

	def test_advances_on_closed_or_held_orders_are_listed_skipped_and_used_up_lines_stuck(self):
		rows = parc_module.get_open_advances("SUP-1", "CO-1", "FUEL", "REQ-9")
		self.assertEqual(
			[(r.name, r.po_status, r.skipped, r.stuck) for r in rows],
			[
				("PARC-B1", "To Receive and Bill", False, False),
				("PARC-B2", "To Receive and Bill", False, False),
				("PARC-A", "Closed", True, False),
				("PARC-C", "On Hold", True, False),
				("PARC-S", "To Bill", False, True),
			],
		)
		allocation.snapshot.assert_called_once_with("SUP-1", "CO-1", "FUEL", for_request="REQ-9")

	def test_an_advance_shows_what_is_held_on_it_and_who_holds_it(self):
		b1, b2 = parc_module.get_open_advances("SUP-1")[:2]
		# PO-1's line has 900 left, 200 of it held (150 on PARC-B1, 50 on the order line itself).
		self.assertEqual((b1.qty_held, b1.qty_available), (150.0, 250.0))
		self.assertEqual((b2.qty_held, b2.qty_available), (0.0, 400.0))
		self.assertEqual(b1.held_by, [{"hold": "REQ-7-v1", "request_id": "REQ-7", "version": 1, "qty": 150.0, "qty_litres": 681.914}])
		self.assertEqual((b1.setup_key, b1.purchase_order_item), ("setup-PO-1", "POI-1"))

	def test_open_purchase_order_lines_come_oldest_order_first_net_of_holds(self):
		rows = parc_module.get_open_purchase_orders("SUP-1")
		self.assertEqual([r.purchase_order for r in rows], ["PO-2", "PO-1"])
		po1 = rows[1]
		self.assertEqual((po1.qty_to_receive, po1.qty_held, po1.qty_available, po1.stuck), (900.0, 200.0, 700.0, False))
		self.assertEqual(po1.qty_available_litres, 3182.263)
		self.assertEqual([h["hold"] for h in po1.held_by], ["REQ-7-v1"])
		parc_module.frappe.has_permission.assert_called_once_with("Purchase Order", "read", throw=True)


class TestWiring(unittest.TestCase):
	def test_hooks_point_at_real_handlers(self):
		paths = [hooks.after_install, hooks.after_migrate]
		paths += [path for events in hooks.doc_events.values() for path in events.values()]
		paths += [path for jobs in hooks.scheduler_events.values() for path in jobs]
		for path in paths:
			module, _dot, attr = path.rpartition(".")
			self.assertTrue(callable(getattr(importlib.import_module(module), attr, None)), path)

	def test_receipt_events_go_the_hold_or_the_desk_way_and_give_back_on_cancel(self):
		events = hooks.doc_events["Purchase Receipt"]
		self.assertEqual(set(events), {"before_insert", "validate", "on_submit", "on_cancel"})
		self.assertEqual(events["validate"], "fuelbuddy_procurement.receipt_events.validate")
		self.assertEqual(events["on_submit"], "fuelbuddy_procurement.receipt_events.on_submit")
		self.assertTrue(events["before_insert"].endswith("receipt_hold.clear_hold_on_amend"))
		self.assertTrue(events["on_cancel"].endswith(".give_back_parcs_on_purchase_receipt_cancel"))

	def test_the_stuck_check_runs_daily(self):
		self.assertEqual(
			hooks.scheduler_events["daily"], ["fuelbuddy_procurement.stuck_advances.stamp_stuck_advances"]
		)

	def test_install_field_links_a_receipt_row_to_one_advance(self):
		(field,) = install.CUSTOM_FIELDS["Purchase Receipt Item"]
		self.assertEqual(
			(field["fieldname"], field["fieldtype"], field["options"]), (PARC_FIELD, "Link", PARC)
		)
		self.assertEqual(field["no_copy"], 1)  # duplicates and returns start without an advance
		self.assertIn([PARC, "docstatus", "=", 0], json.loads(field["link_filters"]))

	def test_install_field_names_the_hold_a_lane_receipt_posts(self):
		(field,) = install.CUSTOM_FIELDS["Purchase Receipt"]
		self.assertEqual(
			(field["fieldname"], field["fieldtype"], field["options"], field["read_only"], field["no_copy"]),
			(receipt_hold.HOLD_FIELD, "Link", allocation.HOLD, 1, 1),
		)

	def test_the_advance_carries_its_consumption_rows_and_its_stuck_stamp(self):
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
		self.assertTrue(set(parc["field_order"]) >= {CONSUMPTIONS, "qty_consumed", "qty_remaining", "stuck_since"})
		self.assertEqual(
			(fields["stuck_since"]["fieldtype"], fields["stuck_since"]["read_only"], fields["stuck_since"]["in_standard_filter"]),
			("Date", 1, 1),
		)
		child = json.loads(
			(DOCTYPES / "purchase_advance_consumption" / "purchase_advance_consumption.json").read_text()
		)
		columns = {field["fieldname"]: field for field in child["fields"]}
		self.assertEqual((child["name"], child["istable"]), (CONSUMPTION, 1))
		# Not a Link: a link to a cancelled receipt would block every later save of the advance.
		self.assertEqual(columns["purchase_receipt"]["fieldtype"], "Data")
		self.assertEqual(columns["is_active"]["default"], "1")
		self.assertEqual(
			(columns["receipt_split_hold"]["fieldtype"], columns["receipt_split_hold"]["options"]),
			("Link", allocation.HOLD),
		)
		self.assertIn("receipt_split_hold", child["field_order"])

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

	def test_receipt_events_module_dispatches_by_hold_field(self):
		self.assertEqual(receipt_events.receipt_hold.HOLD_FIELD, "custom_receipt_split_hold")


if __name__ == "__main__":
	unittest.main()
