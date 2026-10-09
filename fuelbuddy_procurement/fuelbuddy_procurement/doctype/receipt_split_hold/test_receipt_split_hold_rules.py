# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Receipt split hold rules that need no site (IDEV-3334): the availability formula, skipped and
stuck advances, setup groups, the oldest-first pre-fill, rounding to a gallon unit at 0.01, every
refusal, the free order and amounts of a typed split, the API's versions (replay, conflict,
supersede, tombstone, release) on an in-memory hold table, its answers and lock handling, the
posting of a hold, the desk receipt's purchase order rules and site flag, the daily stuck check, and
the DocType definitions. All names and quantities are made up.

Runs from the repository root with plain Python, on the stand-in ``frappe`` of the PARC rules when
the real one cannot be imported:

    python -m unittest fuelbuddy_procurement.fuelbuddy_procurement.doctype.receipt_split_hold.test_receipt_split_hold_rules
"""

import datetime
import json
import pathlib
import types
import unittest
from unittest import mock

from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.test_parc_rules import (
	_install_stand_in_frappe,
)

try:
	import frappe
except ImportError:  # pragma: no cover - test_parc_rules installed it already
	_install_stand_in_frappe()
	import frappe

from fuelbuddy_procurement import allocation, receipt_events, receipt_hold, stuck_advances
from fuelbuddy_procurement.allocation import (
	DUPLICATE_SOURCE,
	PARC_NOT_OPEN,
	PARC_SKIPPED,
	PARC_STUCK,
	PO_NOT_OPEN,
	SETUP_MISMATCH,
	SHORT,
	SOURCE_MISMATCH,
	TOTAL_MISMATCH,
	UOM_FACTOR_MISMATCH,
)
from fuelbuddy_procurement.api import receipt_split_hold as api
from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control import (
	purchase_advance_receipt_control as parc_module,
)
from fuelbuddy_procurement.fuelbuddy_procurement.doctype.receipt_split_hold import (
	receipt_split_hold as hold_module,
)

_dict = frappe._dict
IG = 4.54609
FACTORS = {"Litre": 1.0, "IG": IG}
DAY = datetime.date(2026, 9, 1)
HERE = pathlib.Path(__file__).parent


# ---- a made-up supplier's world ----------------------------------------------------------------------
def order(name, supplier="SUP-1", company="CO-1", status="To Receive and Bill", docstatus=1, date="2026-09-01", created=0, setup="SETUP-A"):
	return _dict(
		name=name,
		supplier=supplier,
		company=company,
		status=status,
		docstatus=docstatus,
		transaction_date=date,
		schedule_date=date,
		creation=f"2026-09-01 08:00:{created:02d}",
		setup_key=setup,
	)


def po_item(name, po, qty=1000.0, received=0.0, uom="IG", factor=IG, item="FUEL", idx=1, stock_uom="Litre"):
	return _dict(
		name=name,
		purchase_order=po,
		idx=idx,
		item_code=item,
		uom=uom,
		stock_uom=stock_uom,
		conversion_factor=factor,
		qty=qty,
		received_qty=received,
	)


def advance(name, po, expected=100.0, consumed=0.0, days=0, created=0, uom="IG", docstatus=0, payment_docstatus=1, purchase_receipt=None):
	return _dict(
		name=name,
		purchase_order=po,
		payment_entry=f"PE-{name}",
		payment_date=DAY + datetime.timedelta(days=days),
		payment_created=datetime.datetime(2026, 9, 1, 8, 0, created),
		payment_docstatus=payment_docstatus,
		docstatus=docstatus,
		purchase_receipt=purchase_receipt,
		uom_of_item=uom,
		advance_paid=1000.0,
		expected=expected,
		consumed=consumed,
	)


def held(hold, line, qty, parc=None, version=1, request=None):
	return _dict(
		name=f"{hold}/{line}/{parc}",
		hold=hold,
		request_id=request or hold.rsplit("-v", 1)[0],
		version=version,
		source_type="PARC" if parc else "PO",
		parc=parc,
		purchase_order_item=line,
		qty=qty,
		qty_litres=round(qty * IG, 3),
	)


def parc_line(no, parc, litres, **fields):
	return _dict(line_no=no, source_type="PARC", parc=parc, purchase_order=fields.get("purchase_order"), purchase_order_item=fields.get("purchase_order_item"), qty_litres=litres)


def po_line(no, po, litres, **fields):
	return _dict(line_no=no, source_type="PO", parc=None, purchase_order=po, purchase_order_item=fields.get("purchase_order_item"), qty_litres=litres)


class World:
	"""Orders, their lines, advances and live hold lines of a made-up supplier SUP-1 (company CO-1, item
	FUEL, gallons at 4.54609 L)."""

	def __init__(self):
		self.orders, self.lines, self.advances, self.held = [], [], [], []

	def add(self, *things):
		for thing in things:
			if "transaction_date" in thing:
				self.orders.append(thing)
			elif "received_qty" in thing:
				self.lines.append(thing)
			elif "expected" in thing:
				self.advances.append(thing)
			else:
				self.held.append(thing)
		return self

	def sources(self, supplier="SUP-1", company="CO-1", item_code="FUEL"):
		return _dict(
			scope=_dict(supplier=supplier, company=company, item_code=item_code),
			orders={o.name: _dict(o) for o in self.orders},
			lines={line.name: _dict(line) for line in self.lines},
			advances={a.name: _dict(a) for a in self.advances},
			queue=[a.name for a in sorted(self.advances, key=allocation.queue_key)],
		)

	def view(self, exclude_hold=None, exclude_request=None, **scope):
		held_lines = [h for h in self.held if h.hold != exclude_hold and h.request_id != exclude_request]
		return allocation.build_view(self.sources(**scope), held_lines)


def codes(refusals):
	return [(r["line_no"], r["code"]) for r in refusals]


def check(world, lines, qty_litres=None, factors=FACTORS, **scope):
	total = qty_litres if qty_litres is not None else round(sum(line.qty_litres for line in lines), 3)
	return allocation.check_split(world.view(**scope), lines, total, factors)


# ---- the formula -------------------------------------------------------------------------------------
class TestFormula(unittest.TestCase):
	def world(self):
		return World().add(
			order("PO-1"),
			po_item("POI-1", "PO-1", qty=1000.0, received=100.0),
			advance("PARC-1", "PO-1", expected=400.0, consumed=50.0),
		)

	def test_an_order_line_has_what_is_left_less_what_holds_hold_of_either_kind(self):
		world = self.world().add(held("R1-v1", "POI-1", 150.0, parc="PARC-1"), held("R2-v1", "POI-1", 50.0))
		line = world.view().lines["POI-1"]
		self.assertEqual((line.left, line.held, line.available), (900.0, 200.0, 700.0))
		self.assertEqual([h.hold for h in line.held_by], ["R1-v1", "R2-v1"])

	def test_an_advance_has_the_lesser_of_what_it_has_left_net_of_its_holds_and_its_line(self):
		world = self.world().add(held("R1-v1", "POI-1", 150.0, parc="PARC-1"))
		adv = world.view().advances["PARC-1"]
		self.assertEqual((adv.remaining, adv.held, adv.available, adv.line), (350.0, 150.0, 200.0, "POI-1"))
		# The line runs short of the advance: its other holds count too.
		world.add(held("R2-v1", "POI-1", 600.0))
		self.assertEqual(world.view().advances["PARC-1"].available, 900.0 - 750.0)

	def test_a_request_or_a_hold_can_be_left_out_of_what_is_held(self):
		world = self.world().add(held("R1-v1", "POI-1", 150.0, parc="PARC-1"))
		self.assertEqual(world.view(exclude_request="R1").advances["PARC-1"].available, 350.0)
		self.assertEqual(world.view(exclude_hold="R1-v1").lines["POI-1"].available, 900.0)

	def test_a_receipt_being_submitted_is_given_back_to_left(self):
		world = self.world()
		view = allocation.build_view(world.sources(), [], received_now={"POI-1": 100.0})
		self.assertEqual(view.lines["POI-1"].left, 1000.0)

	def test_an_advance_books_against_its_orders_first_line_in_its_unit_with_something_left(self):
		world = World().add(
			order("PO-1"),
			po_item("POI-A", "PO-1", qty=500.0, received=500.0, idx=1),
			po_item("POI-B", "PO-1", qty=500.0, idx=2),
			po_item("POI-L", "PO-1", qty=900.0, uom="Litre", factor=1.0, idx=3),
			advance("PARC-1", "PO-1"),
		)
		self.assertEqual(world.view().advances["PARC-1"].line, "POI-B")
		world.lines[1].received_qty = 500.0  # nothing left anywhere: the first line in its unit
		self.assertEqual(world.view().advances["PARC-1"].line, "POI-A")
		world.advances[0].uom_of_item = "Drum"  # no line in its unit
		self.assertIsNone(world.view().advances["PARC-1"].line)

	def test_an_advance_is_skipped_while_its_order_is_closed_or_on_hold(self):
		for status in ("Closed", "On Hold"):
			world = World().add(order("PO-1", status=status), po_item("POI-1", "PO-1"), advance("PARC-1", "PO-1"))
			adv = world.view().advances["PARC-1"]
			self.assertEqual((adv.skipped, adv.stuck), (True, False), status)

	def test_an_advance_is_stuck_when_its_line_has_nothing_left_to_receive(self):
		world = World().add(order("PO-1"), po_item("POI-1", "PO-1", qty=1000.0, received=999.995), advance("PARC-1", "PO-1"))
		self.assertTrue(world.view().advances["PARC-1"].stuck)
		world.lines[0].received_qty = 990.0
		self.assertFalse(world.view().advances["PARC-1"].stuck)
		# A line fully held by others is not stuck: it is short, and says who holds it.
		world.add(held("R1-v1", "POI-1", 10.0))
		adv = world.view().advances["PARC-1"]
		self.assertEqual((adv.stuck, adv.available), (False, 0.0))
		# Nothing left on the advance is not stuck either: it is used up.
		world.lines[0].received_qty = 1000.0
		world.advances[0].consumed = 100.0
		self.assertFalse(world.view().advances["PARC-1"].stuck)

	def test_an_advance_is_open_while_draft_paid_and_with_quantity_left(self):
		world = World().add(
			order("PO-1"),
			po_item("POI-1", "PO-1"),
			advance("PARC-OPEN", "PO-1"),
			advance("PARC-USED", "PO-1", consumed=99.995),
			advance("PARC-CLOSED", "PO-1", docstatus=1),
			advance("PARC-UNPAID", "PO-1", payment_docstatus=2),
		)
		self.assertEqual([a.name for a in allocation.open_advances(world.view())], ["PARC-OPEN"])

	def test_setup_key_groups_orders_one_receipt_can_carry(self):
		base = _dict(company="CO-1", buying_price_list="Standard Buying", tax_category=None, taxes_and_charges="Input VAT")
		taxes = [_dict(account_head="VAT - CO", charge_type="On Net Total", rate=5, add_deduct_tax="Add")]
		key = allocation.setup_key(base, taxes)
		self.assertEqual(key, allocation.setup_key(_dict(base, tax_category=""), [_dict(taxes[0], rate="5.0")]))
		self.assertNotEqual(key, allocation.setup_key(_dict(base, tax_category="VAT"), taxes))
		self.assertNotEqual(key, allocation.setup_key(base, [_dict(taxes[0], rate=0)]))
		self.assertNotEqual(key, allocation.setup_key(_dict(base, company="CO-2"), taxes))
		self.assertNotEqual(key, allocation.setup_key(_dict(base, buying_price_list="Other"), taxes))
		self.assertEqual(len(key), 12)

	def test_oldest_first_is_payment_date_then_entry_then_name(self):
		heads = [
			_dict(name="B", payment_date=DAY, payment_created="2026-09-01 08:00:05"),
			_dict(name="A", payment_date=DAY, payment_created="2026-09-01 08:00:05"),
			_dict(name="C", payment_date=DAY, payment_created="2026-09-01 08:00:01"),
			_dict(name="D", payment_date=DAY + datetime.timedelta(days=1), payment_created="2026-09-01 08:00:00"),
			_dict(name="E", payment_date=None, payment_created=None),
		]
		self.assertEqual([h.name for h in sorted(heads, key=allocation.queue_key)], ["C", "A", "B", "D", "E"])


# ---- the refusals ------------------------------------------------------------------------------------
class TestRefusals(unittest.TestCase):
	"""One test per refusal code; each refusal names its line."""

	def setUp(self):
		self.world = World().add(
			order("PO-1"),
			po_item("POI-1", "PO-1", qty=1000.0),
			advance("PARC-1", "PO-1", expected=100.0),
			order("PO-2", created=1),
			po_item("POI-2", "PO-2", qty=1000.0),
		)

	def refused(self, *lines, qty_litres=None, **scope):
		refusals, _resolved = check(self.world, list(lines), qty_litres, **scope)
		return refusals

	def test_a_split_that_fits_has_no_refusal_and_resolves_every_line(self):
		refusals, resolved = check(self.world, [parc_line(1, "PARC-1", 454.609), po_line(2, "PO-2", 1000.0)])
		self.assertEqual(refusals, [])
		self.assertEqual(
			[(r.line_no, r.purchase_order, r.purchase_order_item, r.uom, r.conversion_factor, round(r.qty, 6), r.qty_litres) for r in resolved],
			[(1, "PO-1", "POI-1", "IG", IG, 100.0, 454.609), (2, "PO-2", "POI-2", "IG", IG, round(1000.0 / IG, 6), 1000.0)],
		)

	def test_parc_not_open(self):
		self.world.add(
			advance("PARC-USED", "PO-1", docstatus=1, purchase_receipt="PR-7"),
			advance("PARC-GONE", "PO-1", docstatus=2),
			advance("PARC-UNPAID", "PO-1", payment_docstatus=0),
			advance("PARC-EMPTY", "PO-1", consumed=100.0),
		)
		refusals = self.refused(
			parc_line(1, "PARC-NONE", 10.0),
			parc_line(2, "PARC-USED", 10.0),
			parc_line(3, "PARC-GONE", 10.0),
			parc_line(4, "PARC-UNPAID", 10.0),
			parc_line(5, "PARC-EMPTY", 10.0),
		)
		self.assertEqual(codes(refusals), [(n, PARC_NOT_OPEN) for n in range(1, 6)])
		self.assertIn("does not exist", refusals[0]["message"])
		self.assertIn("Purchase Receipt PR-7 used the last of it", refusals[1]["message"])
		self.assertIn("is cancelled", refusals[2]["message"])
		self.assertIn("is not submitted", refusals[3]["message"])
		self.assertIn("has nothing left", refusals[4]["message"])
		self.assertTrue(refusals[0]["message"].startswith("Line 1: "))

	def test_parc_skipped(self):
		self.world.orders[0].status = "On Hold"
		(refusal,) = self.refused(parc_line(1, "PARC-1", 10.0))
		self.assertEqual(refusal["code"], PARC_SKIPPED)
		self.assertIn("Purchase Order PO-1, which is On Hold", refusal["message"])

	def test_parc_stuck(self):
		self.world.lines[0].received_qty = 1000.0
		(refusal,) = self.refused(parc_line(1, "PARC-1", 10.0))
		self.assertEqual(refusal["code"], PARC_STUCK)
		self.assertIn("100.000 IG left, but its order line POI-1 has nothing left to receive", refusal["message"])

	def test_po_not_open(self):
		self.world.add(order("PO-DRAFT", docstatus=0), po_item("POI-D", "PO-DRAFT"), order("PO-CLOSED", status="Closed"), po_item("POI-C", "PO-CLOSED"))
		refusals = self.refused(po_line(1, "PO-NONE", 10.0), po_line(2, "PO-DRAFT", 10.0), po_line(3, "PO-CLOSED", 10.0))
		self.assertEqual(codes(refusals), [(1, PO_NOT_OPEN), (2, PO_NOT_OPEN), (3, PO_NOT_OPEN)])
		self.assertIn("is not submitted", refusals[1]["message"])
		self.assertIn("is Closed", refusals[2]["message"])

	def test_source_mismatch(self):
		self.world.add(
			order("PO-OTHER", supplier="SUP-2"),
			po_item("POI-O", "PO-OTHER"),
			advance("PARC-OTHER", "PO-OTHER"),
			order("PO-CO2", company="CO-2"),
			po_item("POI-C2", "PO-CO2"),
			order("PO-DIESEL"),
			po_item("POI-X", "PO-DIESEL", item="OTHER-ITEM"),
		)
		refusals = self.refused(
			parc_line(1, "PARC-OTHER", 10.0),
			po_line(2, "PO-CO2", 10.0),
			parc_line(3, "PARC-1", 10.0, purchase_order="PO-2"),
			po_line(4, "PO-DIESEL", 10.0),
			po_line(5, "PO-2", 10.0, purchase_order_item="POI-1"),
		)
		self.assertEqual(codes(refusals), [(n, SOURCE_MISMATCH) for n in range(1, 6)])
		self.assertIn("advance to supplier SUP-2, not SUP-1", refusals[0]["message"])
		self.assertIn("for company CO-2, not CO-1", refusals[1]["message"])
		self.assertIn("is on Purchase Order PO-1; the line names PO-2", refusals[2]["message"])
		self.assertIn("has no line for item FUEL", refusals[3]["message"])
		self.assertIn("has no line POI-1 for item FUEL", refusals[4]["message"])

	def test_source_mismatch_when_the_advance_unit_is_not_on_its_order(self):
		self.world.advances[0].uom_of_item = "Litre"
		(refusal,) = self.refused(parc_line(1, "PARC-1", 10.0))
		self.assertEqual(refusal["code"], SOURCE_MISMATCH)
		self.assertIn("has no line for item FUEL in Litre", refusal["message"])

	def test_uom_factor_mismatch(self):
		self.world.lines[0].conversion_factor = 4.5
		self.world.add(order("PO-L"), po_item("POI-L", "PO-L", uom="Litre", factor=1.0, stock_uom="Kg"), order("PO-DR"), po_item("POI-DR", "PO-DR", uom="Drum", factor=200.0))
		refusals = self.refused(parc_line(1, "PARC-1", 45.0), po_line(2, "PO-L", 10.0), po_line(3, "PO-DR", 10.0))
		self.assertEqual(codes(refusals), [(1, UOM_FACTOR_MISMATCH), (2, UOM_FACTOR_MISMATCH), (3, UOM_FACTOR_MISMATCH)])
		self.assertIn("books IG at 4.5 L, but FuelBuddy converts it at 4.54609 L", refusals[0]["message"])
		self.assertIn("is stocked in Kg", refusals[1]["message"])
		self.assertIn("is in Drum, which FuelBuddy has no conversion to litres for", refusals[2]["message"])

	def test_uom_factor_mismatch_when_the_unit_changed_since_the_hold_was_placed(self):
		view = self.world.view()
		refusals, _resolved = allocation.check_lines(view, [po_line(1, "PO-2", 10.0)], units={1: ("IG", 4.5)})
		self.assertEqual(codes(refusals), [(1, UOM_FACTOR_MISMATCH)])
		self.assertIn("was IG at 4.5 L when the hold was placed", refusals[0]["message"])

	def test_short_names_what_is_available_and_who_holds_the_rest(self):
		self.world.add(held("R-OTHER-v2", "POI-1", 60.0, parc="PARC-1", version=2))
		(refusal,) = self.refused(parc_line(1, "PARC-1", 200.0))
		self.assertEqual(refusal["code"], SHORT)
		self.assertEqual(refusal["available_litres"], 181.843)  # 40 IG, rounded down to the millilitre
		self.assertEqual([h["hold"] for h in refusal["held_by"]], ["R-OTHER-v2"])
		self.assertIn("advance PARC-1 has 181.843 L available; the line asks 200.000 L (272.765 L held by R-OTHER v2)", refusal["message"])

	def test_lines_on_one_order_line_add_up_against_it(self):
		self.world.lines[0].qty = 150.0  # 100 IG of it the advance's
		refusals = self.refused(parc_line(1, "PARC-1", 454.609), po_line(2, "PO-1", 300.0))
		self.assertEqual(codes(refusals), [(2, SHORT)])
		self.assertIn("has 227.304 L available", refusals[0]["message"])
		self.assertIn("earlier lines of this split take 454.609 L of its order line", refusals[0]["message"])

	def test_duplicate_source(self):
		refusals = self.refused(
			parc_line(1, "PARC-1", 10.0), parc_line(2, "PARC-1", 10.0), po_line(3, "PO-2", 10.0), po_line(4, "PO-2", 10.0)
		)
		self.assertEqual(codes(refusals), [(2, DUPLICATE_SOURCE), (4, DUPLICATE_SOURCE)])

	def test_total_mismatch(self):
		refusals = self.refused(po_line(1, "PO-2", 100.0), qty_litres=100.001)
		self.assertEqual(codes(refusals), [(None, TOTAL_MISMATCH)])
		self.assertEqual(refusals[0]["message"], "The lines add up to 100.000 L; the receipt is 100.001 L")

	def test_setup_mismatch_is_refused_not_designed_around(self):
		self.world.orders[1].setup_key = "SETUP-B"
		refusals = self.refused(parc_line(1, "PARC-1", 100.0), po_line(2, "PO-2", 100.0))
		self.assertEqual(codes(refusals), [(None, SETUP_MISMATCH)])
		self.assertIn("Purchase Orders PO-1 and PO-2 have a different company, price list or tax setup", refusals[0]["message"])


# ---- a typed split: any order, any amounts -----------------------------------------------------------
class TestTypedSplit(unittest.TestCase):
	"""What the Procurement Manager may type: ERP checks every line and the total, never the order of
	the lines or how much each advance takes."""

	def setUp(self):
		self.world = World().add(
			order("PO-1"),
			po_item("POI-1", "PO-1", qty=1000.0),
			advance("PARC-OLD", "PO-1", expected=100.0, days=0),
			order("PO-2", created=1),
			po_item("POI-2", "PO-2", qty=1000.0),
			advance("PARC-NEW", "PO-2", expected=100.0, days=1),
			order("PO-3", created=2),
			po_item("POI-3", "PO-3", qty=1000.0),
		)

	def assertFits(self, *lines):
		refusals, _resolved = check(self.world, list(lines))
		self.assertEqual(refusals, [])

	def test_a_newer_advance_ahead_of_an_older_one(self):
		self.assertFits(parc_line(1, "PARC-NEW", 454.609), parc_line(2, "PARC-OLD", 454.609))

	def test_an_order_line_ahead_of_an_advance(self):
		self.assertFits(po_line(1, "PO-3", 500.0), parc_line(2, "PARC-OLD", 100.0))

	def test_an_advance_not_taking_all_it_could(self):
		self.assertFits(parc_line(1, "PARC-OLD", 100.0), po_line(2, "PO-3", 800.0))

	def test_order_lines_only_while_advances_have_quantity(self):
		self.assertFits(po_line(1, "PO-3", 900.0))

	def test_the_typed_split_is_not_the_oldest_first_pre_fill(self):
		view = self.world.view()
		plan = allocation.prefill(view, 900.0, FACTORS)
		_refusals, typed = allocation.check_split(view, [po_line(1, "PO-3", 900.0)], 900.0, FACTORS)
		_refusals, same = allocation.check_split(view, plan.lines, 900.0, FACTORS)
		self.assertFalse(allocation.same_split(typed, plan.lines))
		self.assertTrue(allocation.same_split(same, plan.lines))


# ---- the pre-fill ------------------------------------------------------------------------------------
class TestPrefill(unittest.TestCase):
	def test_advances_oldest_first_each_taking_all_it_can_then_order_lines_oldest_first(self):
		world = World().add(
			order("PO-B", date="2026-09-05"),
			po_item("POI-B", "PO-B", qty=1000.0),
			order("PO-A", date="2026-09-02"),
			po_item("POI-A", "PO-A", qty=1000.0),
			advance("PARC-2", "PO-B", expected=50.0, days=1),
			advance("PARC-1", "PO-A", expected=100.0, days=0),
		)
		plan = allocation.prefill(world.view(), 3000.0, FACTORS)
		self.assertEqual(
			[(line.source_type, line.parc or line.purchase_order_item, line.qty_litres) for line in plan.lines],
			[("PARC", "PARC-1", 454.609), ("PARC", "PARC-2", 227.304), ("PO", "POI-A", 2318.087)],
		)
		self.assertEqual((plan.total_litres, plan.short_litres, plan.setup_key), (3000.0, 0.0, "SETUP-A"))
		self.assertEqual([line.line_no for line in plan.lines], [1, 2, 3])
		# Every pre-fill line passes the checks.
		refusals, _resolved = allocation.check_split(world.view(), plan.lines, 3000.0, FACTORS)
		self.assertEqual(refusals, [])

	def test_an_advance_takes_no_more_than_its_line_has_and_the_line_keeps_the_rest_for_order_lines(self):
		world = World().add(order("PO-1"), po_item("POI-1", "PO-1", qty=150.0), advance("PARC-1", "PO-1", expected=100.0))
		plan = allocation.prefill(world.view(), 1000.0, FACTORS)
		self.assertEqual([(line.source_type, line.qty_litres) for line in plan.lines], [("PARC", 454.609), ("PO", 227.304)])
		self.assertEqual(plan.short_litres, 318.087)

	def test_skipped_stuck_unit_mismatched_and_held_sources_are_passed_over(self):
		world = World().add(
			order("PO-HOLD", status="On Hold"),
			po_item("POI-H", "PO-HOLD"),
			advance("PARC-SKIPPED", "PO-HOLD", days=0),
			order("PO-DONE"),
			po_item("POI-D", "PO-DONE", qty=10.0, received=10.0),
			advance("PARC-STUCK", "PO-DONE", days=1),
			order("PO-BAD"),
			po_item("POI-BAD", "PO-BAD", factor=4.0),
			advance("PARC-BAD", "PO-BAD", days=2),
			order("PO-OK"),
			po_item("POI-OK", "PO-OK", qty=100.0),
			advance("PARC-HELD", "PO-OK", days=3, expected=100.0),
			held("R1-v1", "POI-OK", 100.0, parc="PARC-HELD"),
		)
		plan = allocation.prefill(world.view(), 50.0, FACTORS)
		self.assertEqual(plan.lines, [])
		self.assertEqual(plan.short_litres, 50.0)

	def test_sources_whose_setup_differs_from_the_first_taken_are_passed_over(self):
		world = World().add(
			order("PO-1", setup="SETUP-A"),
			po_item("POI-1", "PO-1", qty=100.0),
			advance("PARC-1", "PO-1", expected=10.0),
			order("PO-2", setup="SETUP-B", date="2026-08-01"),
			po_item("POI-2", "PO-2", qty=100.0),
			order("PO-3", setup="SETUP-A", date="2026-09-03"),
			po_item("POI-3", "PO-3", qty=100.0),
		)
		plan = allocation.prefill(world.view(), 500.0, FACTORS)
		self.assertEqual([line.purchase_order for line in plan.lines], ["PO-1", "PO-1", "PO-3"])
		self.assertEqual(plan.setup_key, "SETUP-A")

	def test_rounding_to_a_gallon_unit_stays_within_epsilon(self):
		"""100 IG is 454.609 L to the millilitre. 0.009 IG over is float dust; 0.02 IG over is short."""
		world = World().add(order("PO-1"), po_item("POI-1", "PO-1", qty=1000.0), advance("PARC-1", "PO-1", expected=100.0))
		self.assertEqual(allocation.prefill(world.view(), 454.609, FACTORS).lines[0].qty_litres, 454.609)
		fits, _resolved = check(world, [parc_line(1, "PARC-1", 454.65)])  # 100.009 IG
		self.assertEqual(fits, [])
		short, _resolved = check(world, [parc_line(1, "PARC-1", 454.70)])  # 100.020 IG
		self.assertEqual(codes(short), [(1, SHORT)])

	def test_whole_millilitres_never_lose_float_dust(self):
		self.assertEqual(allocation.to_ml(0.1 + 0.2), 300)
		self.assertEqual(allocation.floor_ml(454.609), 454609)
		self.assertEqual(allocation.floor_ml(100 * IG), 454609)


# ---- the API: versions on an in-memory hold table ------------------------------------------------------
class FakeHold:
	"""A Receipt Split Hold document in the in-memory table: fields as attributes."""

	def __init__(self, table, fields):
		self.__dict__["_table"] = table
		self.__dict__["flags"] = types.SimpleNamespace()
		data = {key: value for key, value in fields.items() if key != "doctype"}
		data["lines"] = [_dict(line) for line in data.get("lines") or []]
		data.setdefault("tombstone", 0)
		data.setdefault("fifo", 0)
		self.__dict__["_data"] = data

	def __getattr__(self, key):
		return self._data.get(key)

	def __setattr__(self, key, value):
		self._data[key] = value

	def get(self, key, default=None):
		return self._data.get(key, default)

	def _unique_live(self):
		live = self._data.get("live_request_id")
		if live and any(doc is not self and doc.live_request_id == live for doc in self._table.docs.values()):
			raise frappe.UniqueValidationError(f"live_request_id {live}")

	def insert(self, ignore_permissions=False, ignore_links=False):
		self.name = hold_module.hold_name(self.request_id, self.version)
		if self.name in self._table.docs:
			raise frappe.DuplicateEntryError(self.name)
		self._unique_live()
		self.creation = f"2026-10-08 09:00:{len(self._table.docs):02d}"
		self._table.docs[self.name] = self
		return self

	def save(self, ignore_permissions=False):
		self._unique_live()
		self._table.saved.append((self.name, self.status, self.release_reason))


class FakeTable:
	def __init__(self):
		self.docs, self.saved = {}, []

	def rows(self, request_id):
		docs = sorted((d for d in self.docs.values() if d.request_id == request_id), key=lambda d: d.version)
		return [_dict(name=d.name, version=d.version, status=d.status, tombstone=d.tombstone, lines_hash=d.lines_hash) for d in docs]

	def held_lines(self, exclude_hold=None, exclude_request=None, **kwargs):
		return [
			_dict(
				name=f"{d.name}/{line.line_no}",
				hold=d.name,
				request_id=d.request_id,
				version=d.version,
				source_type=line.source_type,
				parc=line.parc,
				purchase_order_item=line.purchase_order_item,
				qty=line.qty,
				qty_litres=line.qty_litres,
			)
			for d in self.docs.values()
			if d.status == "Held" and d.name != exclude_hold and d.request_id != exclude_request
			for line in d.lines
		]

	def get_doc(self, *args, for_update=False):
		if isinstance(args[0], dict):
			return FakeHold(self, args[0])
		_doctype, name = args
		return self.docs[name]

	def held(self):
		return sorted(name for name, doc in self.docs.items() if doc.status == "Held")


class _ApiCase(unittest.TestCase):
	"""The API against a made-up world (one advance of 100 IG on PO-1, 1000 IG on PO-2) and an
	in-memory hold table."""

	def setUp(self):
		self.world = World().add(
			order("PO-1"),
			po_item("POI-1", "PO-1", qty=1000.0),
			advance("PARC-1", "PO-1", expected=100.0),
			order("PO-2", created=1),
			po_item("POI-2", "PO-2", qty=1000.0),
		)
		self.table = FakeTable()
		self.db = mock.Mock()
		self.db.sql.side_effect = self._sql
		self.db.is_deadlocked.return_value = False
		self.db.is_timedout.return_value = False
		self.lock_wait = [50]
		self.permitted = True
		patches = [
			mock.patch.object(api.allocation, "read_sources", lambda *args, **kwargs: self.world.sources()),
			mock.patch.object(api.allocation, "read_held", lambda sources, **kwargs: self.table.held_lines(**kwargs)),
			mock.patch.object(api, "_lock_request", self.table.rows),
			mock.patch.object(api.frappe, "get_doc", self.table.get_doc, create=True),
			mock.patch.object(api.frappe, "db", self.db, create=True),
			mock.patch.object(api.frappe, "has_permission", lambda doctype, ptype: self.permitted, create=True),
		]
		for patch in patches:
			patch.start()
			self.addCleanup(patch.stop)

	def _sql(self, query, values=None, **kwargs):
		if query.startswith("select @@session.innodb_lock_wait_timeout"):
			return [[self.lock_wait[-1]]]
		if query.startswith("set session innodb_lock_wait_timeout"):
			self.lock_wait.append(values[0])
			return []
		raise AssertionError(query)

	def place(self, version=1, lines=None, request_id="REQ-1", qty_litres=None, **fields):
		lines = lines if lines is not None else [{"line_no": 1, "source_type": "PARC", "parc": "PARC-1", "qty_litres": 300.0}]
		if isinstance(lines, str):  # a malformed case passes its own lines, as JSON text
			total = 10.0
		else:
			total = round(sum(line["qty_litres"] for line in lines), 3)
		total = qty_litres if qty_litres is not None else total
		args = {
			"request_id": request_id,
			"version": version,
			"supplier": "SUP-1",
			"company": "CO-1",
			"item_code": "FUEL",
			"op_key": f"op-{request_id}",
			"qty_litres": total,
			"lines": lines if isinstance(lines, str) else json.dumps(lines),
			"uom_factors": json.dumps(FACTORS),
			**fields,
		}
		return api.place(**args)

	def assertAnswer(self, answer, result):
		self.assertEqual(set(answer), {"ok", "code", "message", "retryable", "result", "hold", "refusals"})
		self.assertTrue(answer["ok"], answer)
		self.assertIsNone(answer["code"])
		self.assertFalse(answer["retryable"])
		self.assertEqual(answer["result"], result, answer["message"])

	def assertRefused(self, answer, code, retryable=False):
		self.assertFalse(answer["ok"], answer)
		self.assertEqual((answer["code"], answer["retryable"], answer["result"], answer["hold"]), (code, retryable, None, None))


class TestPlace(_ApiCase):
	def test_place_holds_its_lines_resolved_and_commits(self):
		answer = self.place()
		self.assertAnswer(answer, "HELD")
		hold = answer["hold"]
		self.assertEqual((hold["name"], hold["status"], hold["live"], hold["version"]), ("REQ-1-v1", "Held", True, 1))
		self.assertEqual(
			hold["lines"],
			[
				{
					"line_no": 1,
					"source_type": "PARC",
					"parc": "PARC-1",
					"purchase_order": "PO-1",
					"purchase_order_item": "POI-1",
					"uom": "IG",
					"conversion_factor": IG,
					"qty": round(300.0 / IG, 9),
					"qty_litres": 300.0,
				}
			],
		)
		self.assertEqual(self.table.docs["REQ-1-v1"].live_request_id, "REQ-1")
		self.db.commit.assert_called_once_with()
		self.db.rollback.assert_not_called()
		self.assertEqual(self.lock_wait, [50, 8, 50])  # 8 s while placing, then as it was

	def test_the_same_version_again_replays_the_answer_and_holds_once(self):
		first = self.place()
		again = self.place()
		self.assertAnswer(again, "HELD")
		self.assertEqual(again["hold"], first["hold"])
		self.assertEqual(self.table.held(), ["REQ-1-v1"])

	def test_the_same_version_with_other_lines_is_a_version_conflict(self):
		self.place()
		answer = self.place(lines=[{"line_no": 1, "source_type": "PO", "purchase_order": "PO-2", "qty_litres": 300.0}])
		self.assertAnswer(answer, "VERSION_CONFLICT")
		self.assertEqual(answer["hold"]["lines"][0]["parc"], "PARC-1")
		self.assertEqual(self.table.held(), ["REQ-1-v1"])

	def test_a_lower_version_than_one_seen_is_superseded(self):
		self.place(version=2)
		answer = self.place(version=1)
		self.assertAnswer(answer, "SUPERSEDED")
		self.assertEqual(answer["hold"]["name"], "REQ-1-v2")
		self.assertEqual(sorted(self.table.docs), ["REQ-1-v2"])

	def test_a_new_version_releases_the_live_one_whatever_its_own_outcome(self):
		self.place(version=1)
		answer = self.place(version=2, lines=[{"line_no": 1, "source_type": "PO", "purchase_order": "PO-NONE", "qty_litres": 300.0}])
		self.assertAnswer(answer, "REJECTED")
		self.assertEqual(codes(answer["refusals"]), [(1, PO_NOT_OPEN)])
		v1 = self.table.docs["REQ-1-v1"]
		self.assertEqual((v1.status, v1.release_reason, v1.live_request_id), ("Released", "REPLACED", None))
		self.assertEqual(self.table.held(), [])

	def test_a_new_version_does_not_count_the_quantity_its_predecessor_held(self):
		self.place(version=1, lines=[{"line_no": 1, "source_type": "PARC", "parc": "PARC-1", "qty_litres": 454.609}])
		answer = self.place(version=2, lines=[{"line_no": 1, "source_type": "PARC", "parc": "PARC-1", "qty_litres": 454.609}])
		self.assertAnswer(answer, "HELD")
		self.assertEqual(self.table.held(), ["REQ-1-v2"])

	def test_two_requests_racing_for_one_advance_one_holds_the_other_is_short_naming_it(self):
		self.assertAnswer(self.place(request_id="REQ-A"), "HELD")
		answer = self.place(request_id="REQ-B")
		self.assertAnswer(answer, "REJECTED")
		(refusal,) = answer["refusals"]
		self.assertEqual((refusal["code"], refusal["available_litres"]), (SHORT, 154.608))  # rounded down
		self.assertEqual([h["request_id"] for h in refusal["held_by"]], ["REQ-A"])
		self.assertEqual(answer["hold"]["status"], "Rejected")
		self.assertIsNone(self.table.docs["REQ-B-v1"].live_request_id)
		# A replay of the rejected version gives the same refusals.
		self.assertEqual(self.place(request_id="REQ-B")["refusals"], answer["refusals"])

	def test_fifo_records_whether_the_split_was_the_oldest_first_pre_fill(self):
		prefill = [
			{"line_no": 1, "source_type": "PARC", "parc": "PARC-1", "qty_litres": 454.609},
			{"line_no": 2, "source_type": "PO", "purchase_order": "PO-1", "qty_litres": 45.391},
		]
		self.assertTrue(self.place(request_id="REQ-F", lines=prefill)["hold"]["fifo"])
		typed = [{"line_no": 1, "source_type": "PO", "purchase_order": "PO-2", "qty_litres": 500.0}]
		self.assertFalse(self.place(request_id="REQ-T", lines=typed)["hold"]["fifo"])

	def test_a_posted_request_takes_no_new_version(self):
		self.place(version=1)
		self.table.docs["REQ-1-v1"].status = "Consumed"
		self.table.docs["REQ-1-v1"].live_request_id = None
		self.table.docs["REQ-1-v1"].purchase_receipt = "PR-POSTED"
		answer = self.place(version=2)
		self.assertAnswer(answer, "CONSUMED")
		self.assertEqual(answer["hold"]["purchase_receipt"], "PR-POSTED")
		self.assertNotIn("REQ-1-v2", self.table.docs)

	def test_a_lock_error_answers_lock_retry_and_stores_nothing(self):
		for error in (frappe.QueryDeadlockError("deadlock"), frappe.QueryTimeoutError("lock wait"), frappe.DuplicateEntryError("dup")):
			with self.subTest(error=type(error).__name__), mock.patch.object(api, "_lock_request", side_effect=error):
				self.db.reset_mock()
				answer = self.place()
				self.assertRefused(answer, "LOCK_RETRY", retryable=True)
				self.db.rollback.assert_called_once_with()
				self.db.commit.assert_not_called()
		self.assertEqual(self.table.docs, {})
		self.assertEqual(self.lock_wait[-1], 50)

	def test_a_racing_second_live_hold_is_refused_by_the_unique_live_key(self):
		self.place(version=1)
		# A second live version slipping past the request's row locks meets the unique live key.
		rogue = self.table.get_doc({"request_id": "REQ-1", "version": 9, "status": "Held", "live_request_id": "REQ-1"})
		with self.assertRaises(frappe.UniqueValidationError):
			rogue.insert()

	def test_a_validation_error_inside_is_erp_validation_and_anything_else_is_raised(self):
		with mock.patch.object(api, "_lock_request", side_effect=frappe.ValidationError("bad")):
			self.assertRefused(self.place(), "ERP_VALIDATION")
		with mock.patch.object(api, "_lock_request", side_effect=RuntimeError("database gone")), self.assertRaises(RuntimeError):
			self.place()

	def test_without_create_on_purchase_receipt_nothing_happens(self):
		self.permitted = False
		self.assertRefused(self.place(), "ERP_VALIDATION")
		self.db.sql.assert_not_called()

	def test_malformed_input_is_erp_validation_before_any_read(self):
		good = {"line_no": 1, "source_type": "PARC", "parc": "PARC-1", "qty_litres": 10.0}
		cases = {
			"request_id": {"request_id": "bad id!"},
			"version": {"version": 0},
			"version text": {"version": "1.5"},
			"supplier": {"supplier": " "},
			"op_key": {"op_key": None},
			"qty_litres": {"qty_litres": "-1"},
			"lines empty": {"lines": "[]"},
			"lines not json": {"lines": "[{"},
			"line_no twice": {"lines": json.dumps([good, good])},
			"source_type": {"lines": json.dumps([dict(good, source_type="ADVANCE")])},
			"parc missing": {"lines": json.dumps([dict(good, parc=None)])},
			"po names a parc": {"lines": json.dumps([dict(good, source_type="PO", purchase_order="PO-2")])},
			"po missing": {"lines": json.dumps([{"line_no": 1, "source_type": "PO", "qty_litres": 1}])},
			"qty zero": {"lines": json.dumps([dict(good, qty_litres=0.0004)])},
			"uom_factors": {"uom_factors": "{}"},
			"uom_factor": {"uom_factors": json.dumps({"IG": 0})},
		}
		for label, fields in cases.items():
			with self.subTest(label):
				self.assertRefused(self.place(**fields), "ERP_VALIDATION")
		self.db.sql.assert_not_called()

	def test_lines_hash_ignores_key_order_and_rounding_but_not_the_split(self):
		def request(**changes):
			base = dict(
				request_id="R", version=1, supplier="S", company="C", item_code="I", op_key="K", qty_litres=10.0,
				lines=[_dict(line_no=1, source_type="PO", parc=None, purchase_order="P", purchase_order_item=None, qty_litres=10.0)],
			)
			return _dict(base, **changes)

		self.assertEqual(api.lines_hash(request()), api.lines_hash(request(qty_litres=10.0001)))
		self.assertNotEqual(api.lines_hash(request()), api.lines_hash(request(op_key="K2")))
		other = [_dict(line_no=1, source_type="PO", parc=None, purchase_order="P2", purchase_order_item=None, qty_litres=10.0)]
		self.assertNotEqual(api.lines_hash(request()), api.lines_hash(request(lines=other)))


class TestRelease(_ApiCase):
	def release(self, up_to=1, reason="REQUEST_CLOSED", request_id="REQ-1"):
		return api.release(request_id=request_id, up_to_version=up_to, reason=reason)

	def test_release_gives_the_live_hold_back_and_twice_is_already_released(self):
		self.place()
		answer = self.release()
		self.assertAnswer(answer, "RELEASED")
		self.assertEqual((answer["hold"]["status"], answer["hold"]["release_reason"]), ("Released", "REQUEST_CLOSED"))
		self.assertEqual(self.table.held(), [])
		self.assertAnswer(self.release(), "ALREADY_RELEASED")

	def test_release_before_place_leaves_a_tombstone_so_the_late_place_holds_nothing(self):
		answer = self.release(up_to=3, reason="APP_WRITE_FAILED")
		self.assertAnswer(answer, "ALREADY_RELEASED")
		self.assertEqual((answer["hold"]["name"], answer["hold"]["tombstone"]), ("REQ-1-v3", True))
		self.assertAnswer(self.place(version=3), "RELEASED")
		self.assertAnswer(self.place(version=2), "SUPERSEDED")
		self.assertEqual(self.table.held(), [])

	def test_release_spares_a_newer_version_in_flight(self):
		self.place(version=2)
		self.assertAnswer(self.release(up_to=1), "ALREADY_RELEASED")
		self.assertEqual(self.table.held(), ["REQ-1-v2"])
		self.assertAnswer(self.release(up_to=2), "RELEASED")

	def test_a_posted_hold_is_not_releasable(self):
		self.place()
		self.table.docs["REQ-1-v1"].status = "Consumed"
		self.table.docs["REQ-1-v1"].purchase_receipt = "PR-POSTED"
		answer = self.release(up_to=5, reason="ORPHAN")
		self.assertAnswer(answer, "NOT_RELEASABLE")
		self.assertIn("PR-POSTED", answer["message"])
		self.assertEqual(sorted(self.table.docs), ["REQ-1-v1"])  # no tombstone either

	def test_only_the_callers_reasons_are_accepted(self):
		self.assertRefused(self.release(reason="REPLACED"), "ERP_VALIDATION")
		self.assertRefused(self.release(up_to="x"), "ERP_VALIDATION")

	def test_release_outcome(self):
		rows = [_dict(version=1, status="Released"), _dict(version=2, status="Held")]
		self.assertEqual(api.release_outcome(rows, 2)[0::2], ("release", False))
		self.assertEqual(api.release_outcome(rows, 1)[0::2], ("already", False))
		self.assertEqual(api.release_outcome(rows, 4)[0::2], ("release", True))
		self.assertEqual(api.release_outcome([], 1)[0::2], ("already", True))


class TestStatusAndSuggest(_ApiCase):
	def test_status_lists_every_version_and_names_the_live_one(self):
		self.place(version=1)
		self.place(version=2)
		with mock.patch.object(api.frappe, "get_all", lambda doctype, filters, pluck, order_by: sorted(self.table.docs), create=True):
			answer = api.status(request_id="REQ-1")
		self.assertAnswer(answer, answer["result"])
		result = answer["result"]
		self.assertEqual((result["live_version"], [v["status"] for v in result["versions"]]), (2, ["Released", "Held"]))
		self.assertEqual(answer["hold"]["name"], "REQ-1-v2")
		self.assertIsNone(result["check"])

	def test_status_check_re_tests_the_live_hold_leaving_itself_out(self):
		self.place(version=1)
		with mock.patch.object(api.frappe, "get_all", lambda doctype, filters, pluck, order_by: sorted(self.table.docs), create=True):
			fits = api.status(request_id="REQ-1", check="1")
			self.assertEqual(fits["result"]["check"], {"ok": True, "refusals": []})
			self.world.orders[0].status = "Closed"  # the order was closed after approval
			stale = api.status(request_id="REQ-1", check="1")
		self.assertFalse(stale["result"]["check"]["ok"])
		self.assertEqual(codes(stale["refusals"]), [(1, PARC_SKIPPED)])

	def test_status_live_lists_every_live_hold(self):
		rows = [_dict(name="REQ-1-v1", request_id="REQ-1", version=1, creation="2026-10-08 09:00:00", supplier="SUP-1", company="CO-1", item_code="FUEL", total_litres=300.0)]
		with mock.patch.object(api.frappe, "get_all", mock.Mock(return_value=rows), create=True) as get_all:
			answer = api.status(live="1")
		self.assertEqual(answer["result"]["live_holds"][0]["name"], "REQ-1-v1")
		self.assertEqual(get_all.call_args.kwargs["filters"], {"status": "Held"})

	def test_suggest_split_lists_the_sources_and_the_pre_fill(self):
		with mock.patch.object(api.allocation, "snapshot", lambda *args, **kwargs: self.world.view()) :
			answer = api.suggest_split(supplier="SUP-1", company="CO-1", item_code="FUEL", qty_litres="1000", uom_factors=json.dumps(FACTORS))
		result = answer["result"]
		self.assertEqual([a["name"] for a in result["advances"]], ["PARC-1"])
		self.assertEqual([line["purchase_order_item"] for line in result["purchase_order_lines"]], ["POI-1", "POI-2"])
		self.assertEqual(
			[(line["source_type"], line["qty_litres"]) for line in result["prefill"]["lines"]],
			[("PARC", 454.609), ("PO", 545.391)],
		)
		self.assertEqual((result["prefill"]["total_litres"], result["prefill"]["short_litres"]), (1000.0, 0.0))
		self.assertIsNone(result["advances"][0]["unit_problem"])

	def test_suggest_split_needs_its_scope(self):
		self.assertRefused(api.suggest_split(supplier="SUP-1", company="CO-1", item_code="FUEL"), "ERP_VALIDATION")


# ---- the hold path: a receipt that posts a hold -----------------------------------------------------------
def hold_view(**changes):
	lines = [
		_dict(line_no=1, source_type="PARC", parc="PARC-1", purchase_order="PO-1", purchase_order_item="POI-1", uom="IG", conversion_factor=IG, qty=100.0, qty_litres=454.609),
		_dict(line_no=2, source_type="PO", parc=None, purchase_order="PO-2", purchase_order_item="POI-2", uom="IG", conversion_factor=IG, qty=50.0, qty_litres=227.305),
	]
	base = _dict(
		name="REQ-1-v1", request_id="REQ-1", version=1, status="Held", supplier="SUP-1", company="CO-1",
		item_code="FUEL", op_key="op-REQ-1", purchase_receipt=None, release_reason=None, lines=lines,
	)
	return _dict(base, **changes)


def lane_receipt(**changes):
	rows = [
		_dict(idx=1, purchase_order="PO-2", purchase_order_item="POI-2", item_code="FUEL", uom="IG", conversion_factor=IG, qty=50.0, received_qty=50.0, name="PRI-1"),
		_dict(idx=2, purchase_order="PO-1", purchase_order_item="POI-1", item_code="FUEL", uom="IG", conversion_factor=IG, qty=100.0, received_qty=100.0, custom_parc="PARC-1", name="PRI-2"),
	]
	base = _dict(
		name="PR-LANE", supplier="SUP-1", company="CO-1", custom_app_op_key="op-REQ-1",
		custom_receipt_split_hold="REQ-1-v1", items=rows, is_return=0, posting_date=DAY, grand_total=1.0,
	)
	return _dict(base, **changes)


class TestHoldPath(unittest.TestCase):
	def test_the_rows_are_the_hold_lines_in_any_row_order(self):
		self.assertEqual(receipt_hold.receipt_problems(lane_receipt(), hold_view()), [])

	def test_each_mismatch_is_named(self):
		receipt = lane_receipt(custom_app_op_key="op-OTHER", supplier="SUP-2")
		receipt["items"][0].qty = 50.02
		receipt["items"][1].uom = "Litre"
		receipt["items"].append(_dict(idx=3, purchase_order="PO-3", purchase_order_item="POI-3", item_code="FUEL", uom="IG", conversion_factor=IG, qty=1.0))
		problems = receipt_hold.receipt_problems(receipt, hold_view())
		self.assertEqual(len(problems), 5, problems)
		self.assertIn("op key is op-OTHER; hold REQ-1-v1 may only be posted by op-REQ-1", problems[0])
		self.assertIn("Hold REQ-1-v1 is for supplier SUP-1, not SUP-2", problems[1])
		self.assertIn("Row 1: books 50.020 IG; hold line 2 holds 50.000", problems[2])
		self.assertIn("Row 2: Litre at 4.54609; hold line 1 is IG at 4.54609", problems[3])
		self.assertIn("Row 3: Purchase Order line POI-3 is not a line of hold REQ-1-v1", problems[4])

	def test_a_missing_row_a_second_row_and_a_wrong_advance_are_named(self):
		receipt = lane_receipt()
		receipt["items"][1].custom_parc = "PARC-OTHER"
		receipt["items"].append(_dict(receipt["items"][0], idx=3))
		problems = receipt_hold.receipt_problems(receipt, hold_view())
		self.assertIn("Row 2: Purchase Order line POI-1 with advance PARC-OTHER is not a line of hold REQ-1-v1", problems[0])
		self.assertIn("Row 3: hold line 2 has more than one row", problems[1])
		self.assertIn("Hold line 1 (Purchase Order line POI-1, advance PARC-1) has no row on this receipt", problems[2])

	def test_only_a_held_hold_may_be_posted(self):
		cases = {
			"Consumed": ("already posted by Purchase Receipt PR-OLD", {"purchase_receipt": "PR-OLD"}),
			"Released": ("was released (POSTING_REFUSED)", {"release_reason": "POSTING_REFUSED"}),
			"Rejected": ("was rejected", {}),
		}
		for status, (text, fields) in cases.items():
			with self.subTest(status):
				(problem,) = receipt_hold.receipt_problems(lane_receipt(), hold_view(status=status, **fields))
				self.assertIn(text, problem)
		self.assertEqual(receipt_hold.receipt_problems(lane_receipt(), None), ["Receipt split hold REQ-1-v1 does not exist"])
		self.assertIn("A return cannot post", receipt_hold.receipt_problems(lane_receipt(is_return=1), hold_view())[0])

	def test_an_amendment_starts_without_the_hold(self):
		amended = _dict(amended_from="PR-LANE", custom_receipt_split_hold="REQ-1-v1")
		amended.set = lambda field, value: amended.__setitem__(field, value)
		receipt_hold.clear_hold_on_amend(amended)
		self.assertIsNone(amended.custom_receipt_split_hold)

	def consume(self, world, current=None):
		current = current or FakeHold(FakeTable(), dict(hold_view(), lines=[]))
		parcs = {"PARC-1": mock.Mock(name="PARC-1")}
		book = mock.Mock()
		with (
			mock.patch.object(receipt_hold, "read_hold", lambda name: hold_view()),
			mock.patch.object(receipt_hold.allocation, "read_sources", lambda *args, **kwargs: world.sources()),
			mock.patch.object(receipt_hold.allocation, "read_held", lambda sources, **kwargs: [h for h in world.held if h.hold != kwargs.get("exclude_hold")]),
			mock.patch.object(receipt_hold.frappe, "get_doc", lambda doctype, name, for_update=False: current if doctype == "Receipt Split Hold" else parcs[name], create=True),
			mock.patch.object(receipt_hold, "book_consumption", book),
		):
			receipt_hold.consume(lane_receipt())
		return current, book, parcs

	def world(self):
		# ERPNext's posting has added this receipt to received_qty already: 100 and 50.
		return World().add(
			order("PO-1"),
			po_item("POI-1", "PO-1", qty=1000.0, received=100.0),
			advance("PARC-1", "PO-1", expected=100.0),
			order("PO-2", created=1),
			po_item("POI-2", "PO-2", qty=1000.0, received=50.0),
			held("REQ-1-v1", "POI-1", 100.0, parc="PARC-1"),  # its own lines: left out
			held("REQ-1-v1", "POI-2", 50.0),
		)

	def test_posting_books_the_advance_lines_tagged_with_the_hold_and_consumes_it(self):
		current, book, parcs = self.consume(self.world())
		book.assert_called_once()
		args, kwargs = book.call_args
		self.assertEqual((args[0], args[2].name, kwargs), (parcs["PARC-1"], "PRI-2", {"hold": "REQ-1-v1"}))
		self.assertEqual((current.status, current.purchase_receipt, current.live_request_id), ("Consumed", "PR-LANE", None))
		self.assertTrue(current.flags.ignore_links)

	def test_posting_is_refused_when_another_hold_took_what_the_line_needs(self):
		world = self.world()
		world.lines[1].qty = 100.0  # 100 received with this receipt's 50: 50 left before it
		world.lines[1].received_qty = 100.0
		world.add(held("REQ-2-v1", "POI-2", 10.0))
		with self.assertRaises(receipt_hold.ReceiptHoldRefusedError) as ctx:
			self.consume(world)
		self.assertIn("Line 2: Purchase Order PO-2 line POI-2 has 181.843 L available; the line asks 227.305 L", str(ctx.exception))

	def test_posting_is_refused_when_the_hold_was_released_meanwhile(self):
		current = FakeHold(FakeTable(), dict(hold_view(status="Released", release_reason="REQUEST_CLOSED"), lines=[]))
		with self.assertRaises(receipt_hold.ReceiptHoldRefusedError) as ctx:
			self.consume(self.world(), current)
		self.assertIn("was released (REQUEST_CLOSED)", str(ctx.exception))
		self.assertEqual(current.status, "Released")

	def test_a_refusal_is_a_parc_refusal_too(self):
		self.assertTrue(issubclass(receipt_hold.ReceiptHoldRefusedError, parc_module.ParcRefusedError))


# ---- the desk path: purchase order lines ---------------------------------------------------------------
class TestDeskPurchaseOrderLines(unittest.TestCase):
	def setUp(self):
		self.lines = {"POI-1": _dict(name="POI-1", purchase_order="PO-1", qty=1000.0, received_qty=900.0, uom="IG")}
		self.holders = {}
		self.conf = _dict()
		patches = [
			mock.patch.object(receipt_events.allocation, "read_lines", lambda names, for_update=False: self.lines),
			mock.patch.object(receipt_events.allocation, "holders_by_line", lambda names, for_update=False: self.holders),
			mock.patch.object(receipt_events.frappe, "conf", self.conf, create=True),
		]
		for patch in patches:
			patch.start()
			self.addCleanup(patch.stop)

	def receipt(self, qty, is_return=0):
		return _dict(items=[_dict(idx=1, purchase_order_item="POI-1", qty=qty, received_qty=qty)], is_return=is_return)

	def test_without_holds_or_the_flag_nothing_is_capped(self):
		self.assertEqual(receipt_events.po_line_problems(self.receipt(500.0)), [])

	def test_a_desk_receipt_never_takes_held_quantity(self):
		self.holders["POI-1"] = [_dict(hold="REQ-7-v1", request_id="REQ-7", version=1, qty=60.0)]
		self.assertEqual(receipt_events.po_line_problems(self.receipt(40.0)), [])
		(problem,) = receipt_events.po_line_problems(self.receipt(40.5))
		self.assertIn("Row 1: Purchase Order PO-1 line POI-1 has 100.000 IG left to receive, 60.000 of it held", problem)
		self.assertIn("(REQ-7 v1); this receipt receives 40.500", problem)

	def test_the_site_flag_caps_every_line_at_what_is_left(self):
		self.conf[receipt_events.DESK_RULES_FLAG] = 1
		self.assertEqual(receipt_events.po_line_problems(self.receipt(100.0)), [])
		(problem,) = receipt_events.po_line_problems(self.receipt(100.02))
		self.assertIn("has 100.000 IG left to receive; this receipt receives 100.020", problem)
		self.assertIn(receipt_events.DESK_RULES_FLAG, problem)

	def test_on_submit_the_receipt_is_already_in_received_qty(self):
		self.conf[receipt_events.DESK_RULES_FLAG] = "1"
		self.lines["POI-1"].received_qty = 1000.0  # 900 + this receipt's 100
		self.assertEqual(receipt_events.po_line_problems(self.receipt(100.0), submitted=True), [])
		self.lines["POI-1"].received_qty = 1001.0
		self.assertEqual(len(receipt_events.po_line_problems(self.receipt(101.0), submitted=True)), 1)

	def test_returns_are_never_capped(self):
		self.conf[receipt_events.DESK_RULES_FLAG] = 1
		self.assertEqual(receipt_events.po_line_problems(self.receipt(5000.0, is_return=1)), [])

	def test_events_send_a_receipt_with_a_hold_the_hold_way_and_any_other_the_desk_way(self):
		with (
			mock.patch.object(receipt_events.receipt_hold, "check_on_save") as hold_save,
			mock.patch.object(receipt_events.receipt_hold, "consume") as hold_submit,
			mock.patch.object(receipt_events.parc_handlers, "check_named_parcs_on_purchase_receipt") as desk_save,
			mock.patch.object(receipt_events.parc_handlers, "consume_named_parcs_on_purchase_receipt") as desk_submit,
		):
			lane = _dict(custom_receipt_split_hold="REQ-1-v1", items=[])
			receipt_events.validate(lane)
			receipt_events.on_submit(lane)
			desk = _dict(items=[])
			receipt_events.validate(desk)
			receipt_events.on_submit(desk)
		hold_save.assert_called_once_with(lane)
		hold_submit.assert_called_once_with(lane)
		desk_save.assert_called_once_with(desk)
		desk_submit.assert_called_once_with(desk)


# ---- the daily stuck check -----------------------------------------------------------------------------
class TestStuckAdvances(unittest.TestCase):
	TODAY = datetime.date(2026, 10, 8)

	def test_stamps_today_on_new_stuck_advances_and_clears_the_unstuck(self):
		stamped = {"PARC-OLD": datetime.date(2026, 10, 1), "PARC-FIXED": datetime.date(2026, 10, 2)}
		changes = stuck_advances.stamp_changes({"PARC-OLD", "PARC-NEW"}, stamped, self.TODAY)
		self.assertEqual(changes, {"PARC-NEW": self.TODAY, "PARC-FIXED": None})

	def state(self, name="PARC-1", since=None):
		return _dict(
			name=name, remaining=40.0, uom_of_item="IG", purchase_order="PO-1", line="POI-1",
			supplier="SUP-1", company="CO-1", stuck_since=since or self.TODAY,
		)

	def test_without_the_lane_issue_fields_it_logs_the_stuck_advances_and_raises_nothing(self):
		with (
			mock.patch.object(stuck_advances, "lane_issues_ready", return_value=False),
			mock.patch.object(stuck_advances.frappe, "log_error", create=True) as log_error,
			mock.patch.object(stuck_advances, "open_issue") as open_issue,
		):
			stuck_advances.flag_purchase([self.state()])
		self.assertIn("PARC-1", log_error.call_args.kwargs["message"])
		open_issue.assert_not_called()

	def test_raises_one_purchase_issue_per_stuck_advance_and_resolves_the_unstuck(self):
		db = mock.Mock()
		db.get_value.return_value = None
		inserted, statuses = [], []

		def get_doc(arg, name=None):
			if isinstance(arg, dict):
				doc = _dict(arg)
				doc.insert = lambda ignore_permissions=False: (inserted.append(doc), _dict(name="ISS-NEW"))[1]
				return doc
			doc = _dict(name=name)
			doc.save = lambda ignore_permissions=False: statuses.append((name, doc.status))
			return doc

		open_issues = [_dict(name="ISS-OLD", custom_app_issue_key="parc-stuck-PARC-FIXED")]
		with (
			mock.patch.object(stuck_advances, "lane_issues_ready", return_value=True),
			mock.patch.object(stuck_advances.frappe, "db", db, create=True),
			mock.patch.object(stuck_advances.frappe, "get_doc", get_doc, create=True),
			mock.patch.object(stuck_advances.frappe, "get_all", mock.Mock(return_value=open_issues), create=True),
		):
			stuck_advances.flag_purchase([self.state()])
		(issue,) = inserted
		self.assertEqual(
			(issue.custom_app_issue_key, issue.custom_lane_team, issue.issue_type),
			("parc-stuck-PARC-1", "Purchase", "Lane Check Mismatch"),
		)
		self.assertIn("Advance PARC-1 is stuck", issue.subject)
		self.assertIn("nothing left to receive", issue.description)
		self.assertEqual(statuses, [("ISS-OLD", "Resolved")])
		self.assertEqual(db.savepoint.call_count, 2)
		self.assertEqual(db.release_savepoint.call_count, 2)

	def test_a_resolved_issue_of_a_still_stuck_advance_is_reopened(self):
		db = mock.Mock()
		db.get_value.return_value = _dict(name="ISS-1", status="Resolved")
		with (
			mock.patch.object(stuck_advances.frappe, "db", db, create=True),
			mock.patch.object(stuck_advances, "_set_status") as set_status,
		):
			self.assertEqual(stuck_advances.open_issue(self.state()), "ISS-1")
		set_status.assert_called_once_with("ISS-1", "Open")

	def test_a_failing_issue_rolls_back_to_its_savepoint_and_the_rest_goes_on(self):
		db = mock.Mock()
		with (
			mock.patch.object(stuck_advances.frappe, "db", db, create=True),
			mock.patch.object(stuck_advances.frappe, "log_error", create=True) as log_error,
		):
			stuck_advances._in_savepoint(mock.Mock(side_effect=RuntimeError("no")), "PARC-1")
		db.rollback.assert_called_once_with(save_point=stuck_advances._SAVEPOINT)
		self.assertIn("PARC-1", log_error.call_args.kwargs["title"])


# ---- the DocTypes ------------------------------------------------------------------------------------
class TestDocTypes(unittest.TestCase):
	def load(self, folder):
		return json.loads((HERE.parent / folder / f"{folder}.json").read_text())

	def test_the_hold_is_named_by_script_read_only_for_the_desk_and_tracked(self):
		doc = self.load("receipt_split_hold")
		self.assertEqual(doc["name"], "Receipt Split Hold")
		self.assertEqual((doc["naming_rule"], doc["in_create"], doc["track_changes"]), ("By script", 1, 1))
		self.assertFalse(doc.get("is_submittable"))
		roles = {perm["role"] for perm in doc["permissions"]}
		self.assertEqual(roles, {"Purchase Manager", "Purchase User", "Stock Manager", "Stock User", "System Manager"})
		for perm in doc["permissions"]:
			self.assertEqual(perm.get("read"), 1)
			for right in ("create", "write", "delete", "submit", "cancel", "amend"):
				self.assertFalse(perm.get(right), (perm["role"], right))
		fields = {field["fieldname"]: field for field in doc["fields"]}
		self.assertEqual(set(doc["field_order"]), set(fields))
		self.assertEqual(fields["status"]["options"].split("\n"), ["Held", "Rejected", "Released", "Consumed"])
		self.assertEqual(fields["live_request_id"]["unique"], 1)
		self.assertEqual(fields["request_id"]["search_index"], 1)
		self.assertEqual(
			fields["release_reason"]["options"].split("\n"),
			["", "REPLACED", "POSTING_REFUSED", "APP_WRITE_FAILED", "REQUEST_CLOSED", "ORPHAN"],
		)
		self.assertEqual(set(fields["release_reason"]["options"].split("\n")[2:]), set(api.RELEASE_REASONS))
		self.assertEqual((fields["lines"]["fieldtype"], fields["lines"]["options"]), ("Table", "Receipt Split Hold Line"))
		self.assertTrue(all(field.get("read_only") for field in doc["fields"] if field["fieldtype"] not in ("Section Break", "Column Break")))

	def test_the_line_carries_its_source_unit_and_quantities(self):
		doc = self.load("receipt_split_hold_line")
		self.assertEqual((doc["name"], doc["istable"]), ("Receipt Split Hold Line", 1))
		fields = {field["fieldname"]: field for field in doc["fields"]}
		self.assertEqual(
			list(doc["field_order"]),
			["line_no", "source_type", "parc", "purchase_order", "purchase_order_item", "uom", "conversion_factor", "qty", "qty_litres"],
		)
		self.assertEqual(fields["source_type"]["options"].split("\n"), ["PARC", "PO"])
		self.assertEqual((fields["parc"]["search_index"], fields["purchase_order_item"]["search_index"]), (1, 1))

	def test_the_name_is_request_and_version_and_a_hold_is_never_deleted(self):
		doc = hold_module.ReceiptSplitHold()
		doc.request_id, doc.version = "REQ-1", 3
		doc.autoname()
		self.assertEqual(doc.name, "REQ-1-v3")
		with self.assertRaises(frappe.ValidationError):
			doc.on_trash()


if __name__ == "__main__":
	unittest.main()
