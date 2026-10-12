# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Receipt split hold races across separate database connections (IDEV-3334).

Each round commits its fixtures (a purchase order of a test supplier and, where needed, a paid
advance), then lets two real sessions (threads with their own Frappe connection, as two web workers
would be) contend for the same quantity while a third "gate" connection holds the contested
purchase order line FOR UPDATE. Once both contenders wait on that lock the gate commits, and the
round checks the outcome:

- two_places_one_advance: two requests ask 60 of an advance's 100. Exactly one is HELD; the other is
  REJECTED with SHORT naming the winner.
- two_places_one_order_line: the same on a purchase order line with 100 left.
- place_vs_desk_receipt: a request asks 60 of the advance while a desk receipt books 60 against it.
  Exactly one wins; the loser is refused (SHORT naming the hold, or the receipt's ParcRefusedError
  naming it).
- place_vs_payment_cancel: a request asks for the advance while its payment is cancelled. Either the
  cancel wins (the request is REJECTED: the advance is gone) or the hold does (the cancel is refused).

Every round also checks that what is held never exceeds what is there. A lock wait or deadlock is a
legitimate outcome for a loser (LOCK_RETRY, or a receipt's deadlock error): the round then retries
the loser once and checks again; nothing may be half-written.

This COMMITS test documents (supplier, purchase orders, payments, advances, receipts, holds) and
leaves them on the site; holds are released at the end of each round but, like every hold, never
deleted. Run it on a lab site only, never on production.

    bench --site <site> execute fuelbuddy_procurement.fuelbuddy_procurement.doctype.receipt_split_hold.test_receipt_split_hold_races.run --kwargs "{'rounds': 50}"
"""

import json
import threading
import time

import frappe
from frappe.utils import flt

from fuelbuddy_procurement.allocation import HOLD
from fuelbuddy_procurement.api import receipt_split_hold as api
from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control import (
	PARC,
	ParcRefusedError,
)
from fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.test_parc_named_advances import (
	_latest_po,
	_new_po,
	_new_supplier,
	_pay,
	_receipt,
)

WAIT_FOR_CONTENDERS = 6  # seconds; place gives up after 8


class Round:
	"""One round's committed fixtures and its two contenders."""

	def __init__(self, template, supplier):
		self.template = template
		self.supplier = supplier
		self.item = template.items[0]
		self.factor = flt(self.item.conversion_factor) or 1.0

	def factors(self):
		return json.dumps({self.item.uom: self.factor, self.item.stock_uom: 1.0})

	def place(self, request_id, lines):
		total = round(sum(line["qty_litres"] for line in lines), 3)
		return api.place(
			request_id=request_id,
			version=1,
			supplier=self.supplier,
			company=self.template.company,
			item_code=self.item.item_code,
			op_key=f"op-{request_id}",
			qty_litres=total,
			lines=json.dumps(lines),
			uom_factors=self.factors(),
		)

	def litres(self, qty):
		return round(qty * self.factor, 3)


def _in_session(site, sites_path, step, results, key):
	"""Run `step` in a session of its own (its own connection), as a web worker would; commits what
	the step leaves uncommitted unless it raised."""
	frappe.init(site=site, sites_path=sites_path)
	frappe.connect()
	try:
		frappe.set_user("Administrator")
		results[key] = step()
		frappe.db.commit()
	except Exception as exc:
		frappe.db.rollback()
		results[key] = exc
	finally:
		frappe.destroy()


def _waiting(count, exclude):
	"""Wait until `count` sessions other than `exclude` (and this one's watcher) run a statement; a
	contender blocked on the gate's lock shows its statement until the lock is free."""
	watcher = frappe.db.create_connection()
	try:
		cursor = watcher.cursor()
		cursor.execute("select connection_id()")
		me = cursor.fetchone()[0]
		deadline = time.monotonic() + WAIT_FOR_CONTENDERS
		while time.monotonic() < deadline:
			cursor.execute(
				"""select count(*) from information_schema.processlist
				where id not in (%s, %s) and command = 'Query' and state != '' and db = database()""",
				(exclude, me),
			)
			if (cursor.fetchone() or [0])[0] >= count:
				return True
			time.sleep(0.05)
		return False
	finally:
		watcher.close()


def contend(doctype, name, first, second):
	"""Hold row `name` of `doctype` from a gate connection while `first` and `second` start in sessions
	of their own; release it once both wait on something (or after WAIT_FOR_CONTENDERS); return both
	results."""
	site, sites_path = frappe.local.site, frappe.local.sites_path
	gate = frappe.db.create_connection()
	cursor = gate.cursor()
	cursor.execute("select connection_id()")
	gate_id = cursor.fetchone()[0]
	cursor.execute(f"select name from `tab{doctype}` where name = %s for update", (name,))
	results = {}
	threads = [
		threading.Thread(target=_in_session, args=(site, sites_path, first, results, "first")),
		threading.Thread(target=_in_session, args=(site, sites_path, second, results, "second")),
	]
	for thread in threads:
		thread.start()
	_waiting(2, gate_id)
	gate.commit()
	gate.close()
	for thread in threads:
		thread.join(timeout=60)
	return results["first"], results["second"]


def _held_total(names):
	"""What live holds hold on these advances or lines, in their unit (a fresh read)."""
	frappe.db.commit()  # a fresh snapshot
	rows = frappe.db.sql(
		f"""select sum(l.qty) from `tab{HOLD} Line` l join `tab{HOLD}` h on h.name = l.parent
		where h.status = 'Held' and (l.parc in %(names)s or (l.source_type = 'PO' and l.purchase_order_item in %(names)s))""",
		{"names": tuple(names)},
	)
	return flt(rows[0][0])


def _release(request_id):
	api.release(request_id=request_id, up_to_version=1, reason="REQUEST_CLOSED")


def _is_lock_loss(outcome):
	if isinstance(outcome, dict):
		return outcome.get("code") == "LOCK_RETRY"
	return isinstance(outcome, frappe.QueryDeadlockError | frappe.QueryTimeoutError)


def _settled(round_, request, ask, outcome):
	"""A place that lost on a lock (LOCK_RETRY) is retried once, as erp-functions does."""
	if _is_lock_loss(outcome):
		outcome = round_.place(request, ask)
		frappe.db.commit()
	return outcome


def _held_and_rejected(round_, ask, first, second, a, b):
	"""(ok, loser) for two places of `ask`: exactly one HELD, the other REJECTED with SHORT naming
	the winner."""
	first, second = _settled(round_, a, ask, first), _settled(round_, b, ask, second)
	if not (isinstance(first, dict) and isinstance(second, dict)):
		return False, (first, second)
	if sorted([first.get("result"), second.get("result")]) != ["HELD", "REJECTED"]:
		return False, (first, second)
	winner, loser = (a, second) if first.get("result") == "HELD" else (b, first)
	named = [holder["request_id"] for refusal in loser["refusals"] for holder in refusal["held_by"]]
	return [refusal["code"] for refusal in loser["refusals"]] == ["SHORT"] and named == [winner], (first, second)


def two_places_one_advance(round_):
	po = _new_po(round_.template, round_.supplier, qty=1000.0)
	(parc,) = _pay(po, 0.1)  # 100
	frappe.db.commit()
	a, b = f"RACE-{frappe.generate_hash(length=8)}", f"RACE-{frappe.generate_hash(length=8)}"
	ask = [{"line_no": 1, "source_type": "PARC", "parc": parc.name, "qty_litres": round_.litres(60.0)}]
	first, second = contend("Purchase Order Item", po.items[0].name, lambda: round_.place(a, ask), lambda: round_.place(b, ask))
	ok, outcomes = _held_and_rejected(round_, ask, first, second, a, b)
	held = _held_total([parc.name])
	ok = ok and held <= 100.0 + 0.01
	for request in (a, b):
		_release(request)
	frappe.db.commit()
	return ok, {"outcomes": outcomes, "held": held}


def two_places_one_order_line(round_):
	po = _new_po(round_.template, round_.supplier, qty=100.0)
	frappe.db.commit()
	a, b = f"RACE-{frappe.generate_hash(length=8)}", f"RACE-{frappe.generate_hash(length=8)}"
	ask = [{"line_no": 1, "source_type": "PO", "purchase_order": po.name, "qty_litres": round_.litres(60.0)}]
	first, second = contend("Purchase Order Item", po.items[0].name, lambda: round_.place(a, ask), lambda: round_.place(b, ask))
	ok, outcomes = _held_and_rejected(round_, ask, first, second, a, b)
	held = _held_total([po.items[0].name])
	ok = ok and held <= 100.0 + 0.01
	for request in (a, b):
		_release(request)
	frappe.db.commit()
	return ok, {"outcomes": outcomes, "held": held}


def place_vs_desk_receipt(round_):
	po = _new_po(round_.template, round_.supplier, qty=1000.0)
	(parc,) = _pay(po, 0.1)  # 100
	desk = _receipt((po, 60.0, parc.name))
	desk.insert()
	frappe.db.commit()
	request = f"RACE-{frappe.generate_hash(length=8)}"
	ask = [{"line_no": 1, "source_type": "PARC", "parc": parc.name, "qty_litres": round_.litres(60.0)}]

	def submit_desk():
		frappe.get_doc("Purchase Receipt", desk.name).submit()
		return "SUBMITTED"

	placed, submitted = contend("Purchase Order Item", po.items[0].name, lambda: round_.place(request, ask), submit_desk)
	frappe.db.commit()
	desk_posted = frappe.db.get_value("Purchase Receipt", desk.name, "docstatus") == 1
	hold_held = isinstance(placed, dict) and placed.get("result") == "HELD"
	refused = isinstance(submitted, ParcRefusedError) or (isinstance(placed, dict) and placed.get("result") == "REJECTED")
	ok = desk_posted != hold_held and refused
	used = flt(frappe.get_doc(PARC, parc.name).qty_consumed) if frappe.db.exists(PARC, parc.name) else 0.0
	ok = ok and used + _held_total([parc.name]) <= 100.0 + 0.01
	_release(request)
	frappe.db.commit()
	return ok, {"placed": placed, "submitted": repr(submitted), "desk_posted": desk_posted}


def place_vs_payment_cancel(round_):
	po = _new_po(round_.template, round_.supplier, qty=1000.0)
	(parc,) = _pay(po, 0.1)
	frappe.db.commit()
	request = f"RACE-{frappe.generate_hash(length=8)}"
	ask = [{"line_no": 1, "source_type": "PARC", "parc": parc.name, "qty_litres": round_.litres(60.0)}]

	def cancel_payment():
		frappe.get_doc("Payment Entry", parc.payment_entry).cancel()
		return "CANCELLED"

	# Gate the advance itself: both the place and the payment cancel lock it.
	placed, cancelled = contend(PARC, parc.name, lambda: round_.place(request, ask), cancel_payment)
	frappe.db.commit()
	gone = not frappe.db.exists(PARC, parc.name)
	hold_held = isinstance(placed, dict) and placed.get("result") == "HELD"
	ok = gone != hold_held and (isinstance(cancelled, frappe.LinkExistsError) or gone)
	_release(request)
	frappe.db.commit()
	return ok, {"placed": placed, "cancelled": repr(cancelled), "advance_gone": gone}


RACES = {
	"two_places_one_advance": two_places_one_advance,
	"two_places_one_order_line": two_places_one_order_line,
	"place_vs_desk_receipt": place_vs_desk_receipt,
	"place_vs_payment_cancel": place_vs_payment_cancel,
}


def run(rounds=50, races=None):
	"""Entry point for ``bench --site <site> execute <this module>.run``. COMMITS test documents: lab
	sites only. Returns {race: {"rounds", "failed", "first_failure"}}."""
	if not frappe.db.exists("DocType", HOLD):
		return {"skipped": "Receipt Split Hold is not on this site: run bench migrate"}
	template = _latest_po()
	report = {}
	for name in races or list(RACES):
		race = RACES[name]
		failed, first_failure = 0, None
		for _round in range(int(rounds)):
			round_ = Round(template, _new_supplier(template))
			frappe.db.commit()
			ok, detail = race(round_)
			if not ok:
				failed += 1
				first_failure = first_failure or repr(detail)[:2000]
		report[name] = {"rounds": int(rounds), "failed": failed, "first_failure": first_failure}
	return report
