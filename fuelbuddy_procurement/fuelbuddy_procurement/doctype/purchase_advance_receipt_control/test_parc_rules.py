# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Checks that need no site: which receipt row may use which advance, how refusals are reported,
the hooks wiring and the install field.

Runs from the repository root with plain Python, using a stand-in ``frappe`` when the real one is
not installed, and under a bench with the real one:

    python -m unittest fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.test_parc_rules
"""

import importlib
import json
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

	def throw(msg, exc=ValidationError, title=None, **kwargs):
		raise exc(msg)

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
	frappe.throw = throw
	frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
	frappe.utils = modules["frappe.utils"]
	frappe.utils.flt = lambda value, precision=None: float(value or 0)
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
	EXPECTED,
	PARC,
	PARC_FIELD,
	ParcRefusedError,
	advance_refusal,
)

_dict = frappe._dict


def _advance(name="PARC-1", **overrides):
	return _dict(
		{
			"name": name,
			"docstatus": 0,
			"purchase_order": "PO-1",
			"purchase_receipt": None,
			"uom_of_item": "IG",
			EXPECTED: 400.0,
			**overrides,
		}
	)


def _closable(name="PARC-1", **overrides):
	"""An advance whose close can be watched: `flags` and `submit()` as on a PARC document."""
	parc = _advance(name, **overrides)
	parc.flags = types.SimpleNamespace()
	parc.submit = mock.Mock()
	return parc


def _row(idx=1, advance="PARC-1", **overrides):
	return _dict(
		{"idx": idx, "purchase_order": "PO-1", "uom": "IG", "qty": 400.0, PARC_FIELD: advance, **overrides}
	)


class TestAdvanceRefusal(unittest.TestCase):
	def refusal(self, parc=None, row=None, advance_supplier="SUP-1"):
		return advance_refusal(parc or _advance(), row or _row(), "SUP-1", advance_supplier)

	def test_open_advance_of_the_same_supplier_po_unit_and_qty_is_used(self):
		self.assertIsNone(self.refusal())

	def test_float_dust_is_ignored(self):
		self.assertIsNone(self.refusal(row=_row(qty=400.009)))
		self.assertIsNone(self.refusal(row=_row(qty=399.991)))

	def test_used_advance_names_the_receipt_that_used_it(self):
		reason = self.refusal(parc=_advance(docstatus=1, purchase_receipt="PR-7"))
		self.assertIn("already used", reason)
		self.assertIn("PR-7", reason)

	def test_cancelled_advance(self):
		self.assertIn("cancelled", self.refusal(parc=_advance(docstatus=2)))

	def test_advance_to_another_supplier(self):
		reason = self.refusal(advance_supplier="SUP-2")
		self.assertIn("SUP-2", reason)
		self.assertIn("SUP-1", reason)

	def test_row_on_another_purchase_order(self):
		reason = self.refusal(row=_row(purchase_order="PO-2"))
		self.assertIn("PO-1", reason)
		self.assertIn("PO-2", reason)
		self.assertIn("no Purchase Order", self.refusal(row=_row(purchase_order=None)))

	def test_row_in_another_unit(self):
		reason = self.refusal(row=_row(uom="Litre"))
		self.assertIn("IG", reason)
		self.assertIn("Litre", reason)

	def test_more_than_the_advance_covers(self):
		self.assertIn("beyond the advance", self.refusal(row=_row(qty=400.02)))

	def test_less_than_the_advance_covers(self):
		self.assertIn("used whole", self.refusal(row=_row(qty=399.98)))


class TestNamedAdvances(unittest.TestCase):
	"""_named_advances: every row at fault is listed, in row order, and PARCs lock in name order."""

	def setUp(self):
		self.advances = {
			"PARC-1": _advance("PARC-1"),
			"PARC-2": _advance("PARC-2", purchase_order="PO-2"),
			"PARC-USED": _advance("PARC-USED", docstatus=1, purchase_receipt="PR-7"),
		}
		self.db = mock.Mock()
		self.db.exists.side_effect = lambda doctype, name: doctype == PARC and name in self.advances
		self.db.get_value.side_effect = lambda doctype, name, field: "SUP-1"
		self.get_doc = mock.Mock(side_effect=lambda doctype, name, for_update=False: self.advances[name])
		patches = [
			mock.patch.object(parc_module.frappe, "db", self.db, create=True),
			mock.patch.object(parc_module.frappe, "get_doc", self.get_doc, create=True),
		]
		for patch in patches:
			patch.start()
			self.addCleanup(patch.stop)

	def receipt(self, *rows, is_return=0, **fields):
		return _dict(items=list(rows), supplier="SUP-1", is_return=is_return, **fields)

	def refused(self, doc):
		with self.assertRaises(ParcRefusedError) as ctx:
			parc_module._named_advances(doc)
		return str(ctx.exception)

	def test_rows_naming_nothing_read_nothing(self):
		self.assertEqual(parc_module._named_advances(self.receipt(_row(advance=None))), [])
		self.db.exists.assert_not_called()
		self.get_doc.assert_not_called()

	def test_a_return_cannot_name_an_advance(self):
		self.assertIn("return", self.refused(self.receipt(_row(), is_return=1)))

	def test_advance_that_does_not_exist(self):
		self.assertIn("does not exist", self.refused(self.receipt(_row(advance="PARC-NONE"))))

	def test_every_row_at_fault_is_listed_in_row_order(self):
		message = self.refused(
			self.receipt(
				_row(idx=1, advance="PARC-USED"),
				_row(idx=2, advance="PARC-1", qty=500.0),
				_row(idx=3, advance="PARC-1"),
				_row(idx=4, advance="PARC-2", purchase_order="PO-2"),
			)
		)
		lines = message.split("<br>")
		self.assertEqual(len(lines), 3, message)
		self.assertTrue(lines[0].startswith("Row 1: advance PARC-USED") and "PR-7" in lines[0], message)
		self.assertTrue(lines[1].startswith("Row 2: advance PARC-1") and "beyond" in lines[1], message)
		self.assertTrue(
			lines[2].startswith("Row 3: advance PARC-1") and "more than one row" in lines[2], message
		)

	def test_submit_locks_named_advances_in_name_order(self):
		first = _row(idx=1, advance="PARC-2", purchase_order="PO-2")
		second = _row(idx=2, advance="PARC-1")
		named = parc_module._named_advances(self.receipt(first, second), for_update=True)
		self.assertEqual(
			self.get_doc.call_args_list,
			[mock.call(PARC, "PARC-1", for_update=True), mock.call(PARC, "PARC-2", for_update=True)],
		)
		self.assertEqual([(parc.name, row.idx) for parc, row in named], [("PARC-1", 2), ("PARC-2", 1)])

	def test_submit_refuses_an_advance_its_locked_read_shows_used(self):
		"""The save check saw the advance open; by submit another receipt has used it. The submit
		goes by its own locked read: it refuses the receipt and closes nothing."""
		open_at_save = _closable("PARC-1")
		used_since = _closable("PARC-1", docstatus=1, purchase_receipt="PR-FIRST")
		self.get_doc.side_effect = lambda doctype, name, for_update=False: (
			used_since if for_update else open_at_save
		)
		doc = self.receipt(_row(), name="PR-SECOND")
		parc_module.check_named_parcs_on_purchase_receipt(doc)
		with self.assertRaises(ParcRefusedError) as ctx:
			parc_module.close_named_parcs_on_purchase_receipt(doc)
		self.assertIn("already used by Purchase Receipt PR-FIRST", str(ctx.exception))
		open_at_save.submit.assert_not_called()
		used_since.submit.assert_not_called()

	def test_submit_closes_each_named_advance_once_with_its_rows_quantity(self):
		closing = self.advances["PARC-1"] = _closable("PARC-1", qty_of_po=1000.0)
		# Received to date on PO-1, this receipt's two rows included.
		self.db.get_value.side_effect = lambda doctype, name, field: (
			407.0 if doctype == "Purchase Receipt Item" else "SUP-1"
		)
		doc = self.receipt(_row(), _row(idx=2, advance=None, qty=7.0), name="PR-9", grand_total=4070.0)
		with mock.patch.object(parc_module.frappe, "msgprint", create=True):
			parc_module.close_named_parcs_on_purchase_receipt(doc)
		self.get_doc.assert_called_once_with(PARC, "PARC-1", for_update=True)
		closing.submit.assert_called_once_with()
		self.assertTrue(closing.flags.ignore_permissions)
		self.assertEqual(
			(
				closing.purchase_receipt,
				closing.qty_of_pr,
				closing.grand_total_of_pr,
				closing.qty_left_to_be_received_from_po,
			),
			("PR-9", 400.0, 4070.0, 593.0),
		)


class TestWiring(unittest.TestCase):
	def test_hooks_point_at_real_handlers(self):
		paths = [hooks.after_install, hooks.after_migrate]
		paths += [path for events in hooks.doc_events.values() for path in events.values()]
		for path in paths:
			module, _dot, attr = path.rpartition(".")
			self.assertTrue(callable(getattr(importlib.import_module(module), attr, None)), path)

	def test_receipt_events_have_no_quantity_matcher(self):
		self.assertEqual(set(hooks.doc_events["Purchase Receipt"]), {"validate", "on_submit", "on_cancel"})

	def test_install_field_links_a_receipt_row_to_one_advance(self):
		(field,) = install.CUSTOM_FIELDS["Purchase Receipt Item"]
		self.assertEqual(
			(field["fieldname"], field["fieldtype"], field["options"]), (PARC_FIELD, "Link", PARC)
		)
		self.assertEqual(field["no_copy"], 1)  # duplicates and returns start without an advance
		self.assertIn([PARC, "docstatus", "=", 0], json.loads(field["link_filters"]))


if __name__ == "__main__":
	unittest.main()
