# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Receipt split holds through real documents, on a site with ERPNext, this app and fuelbuddy_crm (its
Purchase Receipt op key) migrated (IDEV-3334).

Each test makes a supplier of its own, copies the site's latest submitted Purchase Order for it and
pays advances against the copies (the fixtures of the PARC bench suite), then places, releases and
posts holds through fuelbuddy_procurement.api.receipt_split_hold, and books desk receipts.

Nothing is kept. The API commits each call; here every call runs inside a savepoint instead (its
commit is a no-op, its rollback goes back to the savepoint), and FrappeTestCase rolls the whole
class back. Run it on a lab or staging site, never on production: it creates real documents and
holds their locks while it runs. Races across separate connections are in
test_receipt_split_hold_races.py.

    bench --site <site> execute fuelbuddy_procurement.fuelbuddy_procurement.doctype.receipt_split_hold.test_receipt_split_holds.run
"""

import json
import sys
import unittest
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from fuelbuddy_procurement import receipt_events, stuck_advances
from fuelbuddy_procurement.allocation import HOLD, PARC
from fuelbuddy_procurement.api import receipt_split_hold as api
from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control import (
	CONSUMPTIONS,
	ParcRefusedError,
	get_open_advances,
	get_open_purchase_orders,
)
from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.test_parc_named_advances import (
	_latest_po,
	_left,
	_new_po,
	_new_supplier,
	_pay,
	_receipt,
	_state,
	_submit,
)
from fuelbuddy_procurement.install import LEGACY_SERVER_SCRIPTS, create_parc_fields
from fuelbuddy_procurement.receipt_hold import HOLD_FIELD, OP_KEY_FIELD, ReceiptHoldRefusedError


def _unique(prefix):
	return f"{prefix}-{frappe.generate_hash(length=10)}"


class _HoldCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		enabled = [s for s in LEGACY_SERVER_SCRIPTS if frappe.db.get_value("Server Script", s, "disabled") == 0]
		if enabled:
			raise AssertionError(f"Disable the legacy PARC Server Scripts first: {enabled}")
		if not frappe.db.exists("DocType", HOLD):
			raise unittest.SkipTest("Receipt Split Hold is not on this site: run bench migrate")
		meta = frappe.get_meta("Purchase Receipt")
		if not meta.has_field(HOLD_FIELD):
			create_parc_fields()  # the app's own fields; their ALTER TABLE commits before any test data
		if not frappe.get_meta("Purchase Receipt").has_field(OP_KEY_FIELD):
			raise unittest.SkipTest("needs fuelbuddy_crm's Purchase Receipt op key (custom_app_op_key)")
		cls.template = _latest_po()

	def setUp(self):
		self.supplier = _new_supplier(self.template)
		self.company = self.template.company
		self.item = self.template.items[0]

	# ---- fixtures --------------------------------------------------------------------------------
	def po(self, **kwargs):
		return _new_po(self.template, self.supplier, **kwargs)

	def factors(self):
		"""FuelBuddy's units as the template's order line converts them, so the unit check passes."""
		return {self.item.uom: flt(self.item.conversion_factor) or 1.0, self.item.stock_uom: 1.0}

	def litres(self, qty):
		return round(qty * (flt(self.item.conversion_factor) or 1.0), 3)

	# ---- the API, inside a savepoint --------------------------------------------------------------
	def call(self, method, **kwargs):
		"""`method` (an API function) as erp-functions calls it, but rolled back with the test: its
		commit is a no-op and its rollback goes back to a savepoint taken before it."""
		real_rollback = frappe.db.rollback
		frappe.db.savepoint("hold_api_call")
		with (
			mock.patch.object(frappe.db, "commit", lambda *args, **kw: None),
			mock.patch.object(frappe.db, "rollback", lambda *args, **kw: real_rollback(save_point="hold_api_call")),
		):
			return method(**kwargs)

	def place(self, request_id, version, lines, qty_litres=None, op_key=None, **fields):
		total = qty_litres if qty_litres is not None else round(sum(line["qty_litres"] for line in lines), 3)
		return self.call(
			api.place,
			request_id=request_id,
			version=version,
			supplier=fields.get("supplier", self.supplier),
			company=fields.get("company", self.company),
			item_code=fields.get("item_code", self.item.item_code),
			op_key=op_key or f"op-{request_id}",
			qty_litres=total,
			lines=json.dumps(lines),
			uom_factors=json.dumps(self.factors()),
		)

	def release(self, request_id, up_to, reason="REQUEST_CLOSED"):
		return self.call(api.release, request_id=request_id, up_to_version=up_to, reason=reason)

	def status(self, request_id, check=0):
		return self.call(api.status, request_id=request_id, check=check, uom_factors=json.dumps(self.factors()))

	def assertResult(self, answer, result):
		self.assertTrue(answer["ok"], answer)
		self.assertEqual(answer["result"], result, answer["message"])

	def parc_line(self, no, parc, qty):
		return {"line_no": no, "source_type": "PARC", "parc": parc.name, "qty_litres": self.litres(qty)}

	def po_line(self, no, po, qty):
		return {"line_no": no, "source_type": "PO", "purchase_order": po.name, "qty_litres": self.litres(qty)}

	def hold(self, name):
		return frappe.get_doc(HOLD, name)

	def lane_receipt(self, hold, op_key):
		"""The lane's Purchase Receipt for `hold` (an API HOLD): one row per line, as erp-functions
		builds it, naming the hold and carrying its op key."""
		rows = [
			(frappe.get_doc("Purchase Order", line["purchase_order"]), round(line["qty"], 3), line["parc"])
			for line in hold["lines"]
		]
		receipt = _receipt(*rows)
		receipt.set(HOLD_FIELD, hold["name"])
		receipt.set(OP_KEY_FIELD, op_key)
		return receipt

	def assertRefused(self, action, *fragments, exc=ParcRefusedError):
		frappe.db.savepoint("hold_refused")
		try:
			with self.assertRaises(exc) as ctx:
				action()
		finally:
			frappe.db.rollback(save_point="hold_refused")
		for fragment in fragments:
			self.assertIn(fragment, str(ctx.exception))


class TestPlaceAndRelease(_HoldCase):
	def test_place_holds_its_lines_and_a_replay_holds_once(self):
		po = self.po(qty=1000.0)
		(parc,) = _pay(po, 0.4)  # 400
		request = _unique("REQ")
		lines = [self.parc_line(1, parc, 300.0), self.po_line(2, po, 100.0)]
		first = self.place(request, 1, lines)
		self.assertResult(first, "HELD")
		hold = first["hold"]
		self.assertEqual((hold["name"], hold["status"], hold["live"]), (f"{request}-v1", "Held", True))
		self.assertEqual(
			[(line["source_type"], line["purchase_order"], line["purchase_order_item"], line["uom"]) for line in hold["lines"]],
			[("PARC", po.name, po.items[0].name, po.items[0].uom), ("PO", po.name, po.items[0].name, po.items[0].uom)],
		)
		self.assertAlmostEqual(hold["lines"][0]["qty"], 300.0, delta=0.001)
		again = self.place(request, 1, lines)
		self.assertResult(again, "HELD")
		self.assertEqual(again["hold"]["name"], hold["name"])
		self.assertEqual(frappe.get_all(HOLD, filters={"request_id": request, "status": "Held"}, pluck="name"), [hold["name"]])

	def test_the_version_matrix(self):
		po = self.po(qty=1000.0)
		request = _unique("REQ")
		lines = [self.po_line(1, po, 100.0)]
		self.assertResult(self.place(request, 2, lines), "HELD")
		self.assertResult(self.place(request, 2, [self.po_line(1, po, 90.0)]), "VERSION_CONFLICT")
		self.assertResult(self.place(request, 1, lines), "SUPERSEDED")
		self.assertResult(self.release(request, 2), "RELEASED")
		self.assertResult(self.release(request, 2), "ALREADY_RELEASED")
		tombstone = self.release(request, 4, "APP_WRITE_FAILED")
		self.assertResult(tombstone, "ALREADY_RELEASED")
		self.assertTrue(tombstone["hold"]["tombstone"])
		self.assertResult(self.place(request, 4, lines), "RELEASED")
		self.assertResult(self.place(request, 3, lines), "SUPERSEDED")
		self.assertEqual(frappe.get_all(HOLD, filters={"request_id": request, "status": "Held"}), [])

	def test_a_new_version_releases_the_live_one_even_when_it_is_rejected(self):
		po = self.po(qty=1000.0)
		request = _unique("REQ")
		self.place(request, 1, [self.po_line(1, po, 100.0)])
		rejected = self.place(request, 2, [self.po_line(1, po, 5000.0)])
		self.assertResult(rejected, "REJECTED")
		self.assertEqual([r["code"] for r in rejected["refusals"]], ["SHORT"])
		v1 = self.hold(f"{request}-v1")
		self.assertEqual((v1.status, v1.release_reason, v1.live_request_id), ("Released", "REPLACED", None))

	def test_two_requests_for_one_advance_the_second_is_short_naming_the_first(self):
		po = self.po(qty=1000.0)
		(parc,) = _pay(po, 0.4)
		first, second = _unique("REQ"), _unique("REQ")
		self.assertResult(self.place(first, 1, [self.parc_line(1, parc, 300.0)]), "HELD")
		answer = self.place(second, 1, [self.parc_line(1, parc, 300.0)])
		self.assertResult(answer, "REJECTED")
		(refusal,) = answer["refusals"]
		self.assertEqual(refusal["code"], "SHORT")
		self.assertEqual([h["request_id"] for h in refusal["held_by"]], [first])
		self.assertAlmostEqual(refusal["available_litres"], self.litres(100.0), delta=0.002)

	def test_every_line_is_checked_but_not_their_order(self):
		older_po, newer_po = self.po(qty=1000.0), self.po(qty=1000.0)
		(older,) = _pay(older_po, 0.1, days_ago=3)
		(newer,) = _pay(newer_po, 0.1, days_ago=1)
		request = _unique("REQ")
		# A newer advance ahead of an older one, an order line ahead of both, an advance taking part.
		typed = [self.po_line(1, newer_po, 50.0), self.parc_line(2, newer, 100.0), self.parc_line(3, older, 40.0)]
		answer = self.place(request, 1, typed)
		self.assertResult(answer, "HELD")
		self.assertFalse(answer["hold"]["fifo"])
		bad = self.place(_unique("REQ"), 1, typed, qty_litres=self.litres(200.0))
		self.assertResult(bad, "REJECTED")
		self.assertIn("TOTAL_MISMATCH", [r["code"] for r in bad["refusals"]])

	def test_a_split_across_orders_of_different_tax_setup_is_refused(self):
		po_a, po_b = self.po(), self.po()
		frappe.db.set_value("Purchase Order", po_b.name, "tax_category", _unique("Setup"), update_modified=False)
		answer = self.place(_unique("REQ"), 1, [self.po_line(1, po_a, 10.0), self.po_line(2, po_b, 10.0)])
		self.assertResult(answer, "REJECTED")
		self.assertEqual([r["code"] for r in answer["refusals"]], ["SETUP_MISMATCH"])

	def test_suggest_split_and_the_lookups_show_what_is_held_and_by_whom(self):
		po = self.po(qty=1000.0)
		(parc,) = _pay(po, 0.4)
		request = _unique("REQ")
		self.place(request, 1, [self.parc_line(1, parc, 150.0), self.po_line(2, po, 50.0)])
		(adv,) = get_open_advances(self.supplier, self.company, self.item.item_code)
		self.assertAlmostEqual(adv.qty_held, 150.0, delta=0.001)
		self.assertAlmostEqual(adv.qty_available, 250.0, delta=0.001)
		self.assertEqual([h["request_id"] for h in adv.held_by], [request])
		(line,) = get_open_purchase_orders(self.supplier, self.company, self.item.item_code)
		self.assertAlmostEqual(line.qty_held, 200.0, delta=0.001)
		self.assertAlmostEqual(line.qty_available, 800.0, delta=0.001)
		# The request's own hold is given back for its re-approval.
		(adv,) = get_open_advances(self.supplier, self.company, self.item.item_code, request)
		self.assertAlmostEqual(adv.qty_available, 400.0, delta=0.001)
		suggestion = self.call(
			api.suggest_split,
			supplier=self.supplier,
			company=self.company,
			item_code=self.item.item_code,
			qty_litres=self.litres(500.0),
			for_request=request,
			uom_factors=json.dumps(self.factors()),
		)
		prefill = suggestion["result"]["prefill"]
		self.assertEqual([line["source_type"] for line in prefill["lines"]], ["PARC", "PO"])
		self.assertAlmostEqual(prefill["lines"][0]["qty_litres"], self.litres(400.0), delta=0.002)
		self.assertEqual(prefill["short_litres"], 0.0)


class TestPosting(_HoldCase):
	def held(self, lines, request=None):
		request = request or _unique("REQ")
		op_key = _unique("erp-grn-test")
		answer = self.place(request, 1, lines, op_key=op_key)
		self.assertResult(answer, "HELD")
		return answer["hold"], op_key

	def test_posting_consumes_the_hold_and_books_its_advances_tagged_with_it(self):
		po = self.po(qty=1000.0)
		(parc,) = _pay(po, 0.4)
		hold, op_key = self.held([self.parc_line(1, parc, 400.0), self.po_line(2, po, 100.0)])
		self.assertTrue(self.status(hold["request_id"], check=1)["result"]["check"]["ok"])
		receipt = _submit(self.lane_receipt(hold, op_key))
		consumed = self.hold(hold["name"])
		self.assertEqual((consumed.status, consumed.purchase_receipt, consumed.live_request_id), ("Consumed", receipt.name, None))
		self.assertEqual(_state(parc), (1, receipt.name))  # used up: closed
		rows = frappe.get_doc(PARC, parc.name).get(CONSUMPTIONS)
		self.assertEqual([(row.purchase_receipt, row.receipt_split_hold) for row in rows], [(receipt.name, hold["name"])])
		self.assertResult(self.release(hold["request_id"], 1), "NOT_RELEASABLE")

	def test_a_receipt_whose_rows_are_not_the_hold_is_refused(self):
		po = self.po(qty=1000.0)
		hold, op_key = self.held([self.po_line(1, po, 100.0)])
		receipt = self.lane_receipt(hold, op_key)
		receipt.items[0].qty = receipt.items[0].received_qty = 90.0
		self.assertRefused(receipt.insert, "hold line 1 holds", exc=ReceiptHoldRefusedError)
		other = self.lane_receipt(hold, _unique("erp-grn-test"))
		self.assertRefused(other.insert, "may only be posted by", exc=ReceiptHoldRefusedError)

	def test_a_released_hold_cannot_be_posted(self):
		po = self.po(qty=1000.0)
		hold, op_key = self.held([self.po_line(1, po, 100.0)])
		receipt = self.lane_receipt(hold, op_key)
		receipt.insert()
		self.release(hold["request_id"], 1, "REQUEST_CLOSED")
		self.assertRefused(receipt.submit, "was released (REQUEST_CLOSED)", exc=ReceiptHoldRefusedError)

	def test_a_closed_order_shows_in_the_check_before_posting(self):
		from erpnext.buying.doctype.purchase_order.purchase_order import update_status

		po = self.po(qty=1000.0)
		hold, _op_key = self.held([self.po_line(1, po, 100.0)])
		update_status("Closed", po.name)
		check = self.status(hold["request_id"], check=1)["result"]["check"]
		self.assertEqual((check["ok"], [r["code"] for r in check["refusals"]]), (False, ["PO_NOT_OPEN"]))

	def test_cancelling_the_receipt_gives_back_the_quantity_and_the_hold_stays_consumed(self):
		po = self.po(qty=1000.0)
		(parc,) = _pay(po, 0.4)
		hold, op_key = self.held([self.parc_line(1, parc, 100.0)])
		receipt = _submit(self.lane_receipt(hold, op_key))
		self.assertAlmostEqual(_left(parc), 300.0, delta=0.01)
		receipt.cancel()
		self.assertAlmostEqual(_left(parc), 400.0, delta=0.01)
		self.assertEqual(self.hold(hold["name"]).status, "Consumed")

	def test_three_advances_and_two_order_lines_on_one_receipt(self):
		po_1, po_2, po_3 = self.po(qty=1000.0), self.po(qty=1000.0), self.po(qty=1000.0)
		(a1,) = _pay(po_1, 0.1, days_ago=3)
		(a2,) = _pay(po_2, 0.1, days_ago=2)
		(a3,) = _pay(po_3, 0.1, days_ago=1)
		lines = [
			self.parc_line(1, a1, 100.0),
			self.parc_line(2, a2, 100.0),
			self.parc_line(3, a3, 60.0),
			self.po_line(4, po_1, 200.0),
			self.po_line(5, po_2, 50.0),
		]
		hold, op_key = self.held(lines)
		receipt = _submit(self.lane_receipt(hold, op_key))
		self.assertEqual(len(receipt.items), 5)
		self.assertEqual([_state(a)[0] for a in (a1, a2)], [1, 1])
		self.assertAlmostEqual(_left(a3), 40.0, delta=0.01)
		self.assertEqual(self.hold(hold["name"]).status, "Consumed")


class TestDeskAndPayment(_HoldCase):
	def test_a_desk_receipt_cannot_take_held_quantity_on_an_advance_or_an_order_line(self):
		po = self.po(qty=500.0)
		(parc,) = _pay(po, 0.4)  # 200
		self.place(_unique("REQ"), 1, [self.parc_line(1, parc, 150.0), self.po_line(2, po, 300.0)])
		self.assertRefused(_receipt((po, 60.0, parc.name)).insert, "150.000 of it held")
		_submit(_receipt((po, 50.0, parc.name)))  # what the advance has beyond the hold
		self.assertRefused(_receipt((po, 1.0, None)).insert, "cannot take held quantity")

	def test_the_site_flag_caps_desk_receipts_at_what_is_left(self):
		po = self.po(qty=100.0)
		with mock.patch.dict(frappe.local.conf, {receipt_events.DESK_RULES_FLAG: 0}):
			_submit(_receipt((po, 60.0, None)))
		with mock.patch.dict(frappe.local.conf, {receipt_events.DESK_RULES_FLAG: 1}):
			self.assertRefused(_receipt((po, 41.0, None)).insert, "Receiving more than is left is switched off")
			_submit(_receipt((po, 40.0, None)))

	def test_a_payment_cannot_be_cancelled_while_a_hold_holds_its_advance(self):
		po = self.po(qty=1000.0)
		(parc,) = _pay(po, 0.4)
		request = _unique("REQ")
		self.place(request, 1, [self.parc_line(1, parc, 100.0)])

		def cancel_payment():
			frappe.get_doc("Payment Entry", parc.payment_entry).cancel()

		self.assertRefused(cancel_payment, f"Advance {parc.name} is held for receipt split hold {request}-v1", exc=frappe.LinkExistsError)
		self.release(request, 1)
		cancel_payment()
		self.assertFalse(frappe.db.exists(PARC, parc.name))


class TestStuck(_HoldCase):
	def test_a_stuck_advance_is_refused_stamped_and_flagged(self):
		po = self.po(qty=100.0)
		(parc,) = _pay(po, 0.5)  # 50
		_submit(_receipt((po, 100.0, None)))  # the order line received in full without the advance
		answer = self.place(_unique("REQ"), 1, [self.parc_line(1, parc, 10.0)])
		self.assertEqual([r["code"] for r in answer["refusals"]], ["PARC_STUCK"])
		(adv,) = get_open_advances(self.supplier)
		self.assertTrue(adv.stuck)
		with mock.patch.object(stuck_advances, "flag_purchase") as flag:
			stuck_advances.stamp_stuck_advances()
		self.assertIsNotNone(frappe.db.get_value(PARC, parc.name, "stuck_since"))
		self.assertIn(parc.name, [state.name for state in flag.call_args.args[0]])


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
