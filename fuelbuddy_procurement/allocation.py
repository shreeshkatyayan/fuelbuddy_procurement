# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""What a supplier's advances (PARC) and purchase order lines have available for a fuel receipt, net
of receipt split holds (IDEV-3334). One formula, used by the hold API, the lookups, desk receipts,
the posting of a hold and the daily stuck check.

Quantities are in the purchase order line's unit, which is also the unit its advance is counted in;
litres are that quantity times the line's conversion_factor. Two quantities closer than QTY_EPSILON
(0.01, in the line's unit) are equal. Litres are compared at 3 decimals (whole millilitres).

    left(I)      = I.qty - I.received_qty                  purchase order line I
    held(I)      = live hold lines on I, advance lines and order lines alike
    available(I) = left(I) - held(I)
    remaining(P) = advance P's quantity minus its active consumption rows
    held(P)      = live hold lines naming P
    available(P) = min(remaining(P) - held(P), available(I_P))       I_P: the line P books against

A live hold is a Receipt Split Hold in status Held. The holds of one request (``exclude_request``)
or one hold (``exclude_hold``) can be left out, so that request or hold sees what it would give
back.

- Scope: one supplier, one company, one item. A PARC has no item: it books against a line of its
  Purchase Order in the advance's unit (and, in an item scope, for that item); the first such line
  with something left to receive, else the first such line (``advance_line``).
- Skipped advance: its Purchase Order is Closed or On Hold; ERPNext takes no receipt against it.
- Stuck advance: not skipped, remaining > QTY_EPSILON, and its line has nothing left to receive
  (left <= QTY_EPSILON). No receipt can use it until Purchase sorts out the order or the advance.
- Setup: one receipt carries one company, price list and tax setup, so every Purchase Order a
  split names must share one ``setup_key``.

Reads come in two kinds. Lookups and suggest_split read a snapshot, without locks. place, the
posting of a hold and a desk submit read under locks, always in this order, so that no two of them
wait on each other the other way round:

  1. Purchase Order Item rows, FOR UPDATE, by name;
  2. their Purchase Orders, LOCK IN SHARE MODE, by name;
  3. advances (PARC), FOR UPDATE, oldest first (``queue_key``), then their active consumption
     rows, LOCK IN SHARE MODE;
  4. (the caller) the hold rows it changes, FOR UPDATE;
  5. live hold lines, LOCK IN SHARE MODE.

A locking read sees the latest committed rows. A plain read in a transaction sees its snapshot,
which can be older than a hold another transaction committed while this one waited for a lock.
"""

import hashlib
import json
import math

import frappe
from frappe import _
from frappe.utils import flt

PARC = "Purchase Advance Receipt Control"
# Child table of PARC (field CONSUMPTIONS): one row per receipt that booked quantity against it.
CONSUMPTION = "Purchase Advance Consumption"
CONSUMPTIONS = "consumptions"
EXPECTED = "qty_to_be_received_against_the_advance_paid"
HOLD = "Receipt Split Hold"
HOLD_LINE = "Receipt Split Hold Line"

# Quantities closer than this, in the purchase order line's unit, are treated as equal (float dust).
QTY_EPSILON = 0.01
# Two unit conversion factors are the same within this (as erp-functions' IDEV-3301 unit guard).
UNIT_TOLERANCE = 1e-9
# ERPNext refuses a Purchase Receipt against a Purchase Order in these states.
PO_STATUSES_TAKING_NO_RECEIPT = ("Closed", "On Hold")

# Receipt Split Hold statuses. Only Held holds quantity.
HELD = "Held"
REJECTED = "Rejected"
RELEASED = "Released"
CONSUMED = "Consumed"

# Receipt Split Hold Line source types.
PARC_LINE = "PARC"
PO_LINE = "PO"

# Refusal codes: per line ...
PARC_NOT_OPEN = "PARC_NOT_OPEN"
PARC_SKIPPED = "PARC_SKIPPED"
PARC_STUCK = "PARC_STUCK"
PO_NOT_OPEN = "PO_NOT_OPEN"
SOURCE_MISMATCH = "SOURCE_MISMATCH"
UOM_FACTOR_MISMATCH = "UOM_FACTOR_MISMATCH"
SHORT = "SHORT"
DUPLICATE_SOURCE = "DUPLICATE_SOURCE"
# ... and for the split as a whole.
TOTAL_MISMATCH = "TOTAL_MISMATCH"
SETUP_MISMATCH = "SETUP_MISMATCH"


# ---- numbers ---------------------------------------------------------------------------------------
def to_ml(litres):
	"""Litres as whole millilitres, so 3-decimal comparisons never meet float dust."""
	return int(round(flt(litres) * 1000))


def from_ml(millilitres):
	return millilitres / 1000


def floor_ml(litres):
	"""Litres rounded down to whole millilitres: what is offered stays inside what is available."""
	return int(math.floor(flt(litres) * 1000 + 1e-6))


def factor_of(line):
	"""The line's conversion factor to litres; 1 when it has none (as erp-functions reads it)."""
	return flt(line.get("conversion_factor")) or 1.0


# ---- order -----------------------------------------------------------------------------------------
def queue_key(adv):
	"""Oldest advance first: the payment's posting date, then the order the payments were entered,
	then the advance's name (which only orders the advances of one payment). An advance without a
	payment goes last."""
	return (
		adv.get("payment_date") is None,
		str(adv.get("payment_date") or ""),
		str(adv.get("payment_created") or ""),
		adv.get("name") or "",
	)


def line_key(line, order):
	"""Oldest purchase order first: order date, then the order they were entered, then name and line."""
	return (
		str(order.get("transaction_date") or ""),
		str(order.get("creation") or ""),
		line.get("purchase_order") or "",
		int(line.get("idx") or 0),
	)


def setup_key(order, taxes=()):
	"""A short key for what one receipt has to carry alike for all its orders: company, price list, tax
	category and template, and the tax rows (account, charge type, rate, add or deduct). Orders that
	differ in any of them need separate receipts (erp-functions' orderSetupMismatch reads the same)."""
	setup = [
		order.get("company") or None,
		order.get("buying_price_list") or None,
		order.get("tax_category") or None,
		order.get("taxes_and_charges") or None,
		[
			[
				row.get("account_head") or None,
				row.get("charge_type") or None,
				flt(row.get("rate")),
				row.get("add_deduct_tax") or None,
			]
			for row in taxes
		],
	]
	return hashlib.sha256(json.dumps(setup, default=str).encode()).hexdigest()[:12]


# ---- the formula -----------------------------------------------------------------------------------
def advance_line(order_lines, uom, item_code=None):
	"""The line of its Purchase Order an advance books against, out of `order_lines` (by position):
	in the advance's unit and, in an item scope, for that item; the first such line with something
	left to receive, else the first such line; None when the order has none."""
	candidates = [
		line for line in order_lines if line.uom == uom and (not item_code or line.item_code == item_code)
	]
	return next((line for line in candidates if line.left > QTY_EPSILON), candidates[0] if candidates else None)


def order_line(order_lines, item_code=None, named=None):
	"""The purchase order line an order line of a split books against: `named` when given (it must be
	one of `order_lines` and, in an item scope, for the item), else the first line for the item with
	something left to receive, else the first line for the item."""
	candidates = [line for line in order_lines if not item_code or line.item_code == item_code]
	if named:
		return next((line for line in candidates if line.name == named), None)
	return next((line for line in candidates if line.left > QTY_EPSILON), candidates[0] if candidates else None)


def is_skipped(order):
	return (order or {}).get("status") in PO_STATUSES_TAKING_NO_RECEIPT


def is_stuck(remaining, line, skipped):
	"""An advance with quantity left whose line has nothing left to receive (and is not skipped)."""
	return not skipped and remaining > QTY_EPSILON and line is not None and line.left <= QTY_EPSILON


def unit_problem(line, uom_factors=None):
	"""Why `line` would book litres at a factor other than FuelBuddy's, or None. `uom_factors`: ERP
	unit -> FuelBuddy's litres per unit, as erp-functions reads them (IDEV-3301); without it there is
	nothing to compare. The stock unit must count in litres (factor 1)."""
	if not uom_factors:
		return None
	stock = uom_factors.get(line.get("stock_uom"))
	if stock is None or abs(flt(stock) - 1) > UNIT_TOLERANCE:
		return _("is stocked in {0}, which FuelBuddy does not count in litres").format(line.get("stock_uom"))
	ours = uom_factors.get(line.get("uom"))
	if ours is None:
		return _("is in {0}, which FuelBuddy has no conversion to litres for").format(line.get("uom"))
	factor = flt(line.get("conversion_factor"))
	if factor <= 0:
		return _("has no usable conversion factor ({0})").format(line.get("conversion_factor"))
	if abs(factor - flt(ours)) > UNIT_TOLERANCE:
		return _("books {0} at {1} L, but FuelBuddy converts it at {2} L").format(line.get("uom"), factor, ours)
	return None


def unit_changed(line, unit):
	"""Why `line` no longer has the unit and factor `unit` = (uom, conversion_factor) a hold resolved,
	or None."""
	if not unit:
		return None
	uom, factor = unit
	if line.get("uom") != uom or abs(flt(line.get("conversion_factor")) - flt(factor)) > UNIT_TOLERANCE:
		return _("was {0} at {1} L when the hold was placed; it is now {2} at {3} L").format(
			uom, flt(factor), line.get("uom"), flt(line.get("conversion_factor"))
		)
	return None


def _holder(row):
	return frappe._dict(
		hold=row.hold, request_id=row.request_id, version=row.version, qty=0.0, qty_litres=0.0
	)


def _add_holder(holders, row):
	"""Adds hold line `row` to `holders` ([holder]), one entry per hold."""
	found = next((h for h in holders if h.hold == row.hold), None)
	if found is None:
		found = _holder(row)
		holders.append(found)
	found.qty += flt(row.qty)
	found.qty_litres = round(found.qty_litres + flt(row.qty_litres), 3)


def build_view(sources, held=(), received_now=None):
	"""Availability of every source in `sources` (``read_sources``) net of `held` (``read_held``).

	`received_now`: {purchase order line: quantity} that a receipt being submitted has already added
	to received_qty; it is given back, so that left() is what the line had before that receipt.

	Returns a _dict: scope, orders, lines {name: line}, by_order {order: [line by position]},
	advances {name: advance}, queue [advance names, oldest first]. A line gains left, held, held_by
	and available; an advance gains remaining, held, held_by, line, skipped, stuck, available, open
	and setup_key."""
	scope = sources.scope
	received_now = received_now or {}
	lines = {}
	for name, row in sources.lines.items():
		line = frappe._dict(row)
		line.left = flt(row.qty) - flt(row.received_qty) + flt(received_now.get(name))
		line.held = 0.0
		line.held_by = []
		lines[name] = line
	held_on_parc = {}
	for row in held:
		line = lines.get(row.purchase_order_item)
		if line is not None:
			line.held += flt(row.qty)
			_add_holder(line.held_by, row)
		if row.source_type == PARC_LINE and row.parc:
			held_on_parc.setdefault(row.parc, []).append(row)
	for line in lines.values():
		line.available = line.left - line.held

	by_order = {}
	for line in sorted(lines.values(), key=lambda line: (line.purchase_order, int(line.idx or 0))):
		by_order.setdefault(line.purchase_order, []).append(line)

	advances = {}
	for name in sources.queue:
		row = sources.advances.get(name)
		if row is None:  # gone since the queue was read
			continue
		adv = frappe._dict(row)
		order = sources.orders.get(adv.purchase_order) or frappe._dict()
		line = advance_line(by_order.get(adv.purchase_order, []), adv.uom_of_item, scope.item_code)
		adv.remaining = flt(adv.expected) - flt(adv.consumed)
		adv.held = 0.0
		adv.held_by = []
		for hold_row in held_on_parc.get(name, []):
			adv.held += flt(hold_row.qty)
			_add_holder(adv.held_by, hold_row)
		adv.line = line.name if line else None
		adv.skipped = is_skipped(order)
		adv.stuck = is_stuck(adv.remaining, line, adv.skipped)
		adv.available = min(adv.remaining - adv.held, line.available) if line else 0.0
		adv.open = adv.docstatus == 0 and adv.payment_docstatus == 1 and adv.remaining > QTY_EPSILON
		adv.setup_key = order.get("setup_key")
		advances[name] = adv
	return frappe._dict(
		scope=scope,
		orders=sources.orders,
		lines=lines,
		by_order=by_order,
		advances=advances,
		queue=[name for name in sources.queue if name in advances],
	)


def _in_scope(order, scope):
	return bool(order) and order.supplier == scope.supplier and (not scope.company or order.company == scope.company)


def open_advances(view):
	"""The scope's open advances, oldest first: draft, on a submitted payment, with quantity left, on
	an order of the supplier (and company). Skipped and stuck ones are included, flagged."""
	for name in view.queue:
		adv = view.advances[name]
		if adv.open and _in_scope(view.orders.get(adv.purchase_order), view.scope):
			yield adv


def open_lines(view):
	"""The scope's open purchase order lines, oldest order first: on a submitted order of the supplier
	(and company) that is not Closed or On Hold, with something left to receive (and, in an item
	scope, for the item)."""
	found = []
	for line in view.lines.values():
		order = view.orders.get(line.purchase_order)
		if not _in_scope(order, view.scope) or order.docstatus != 1 or is_skipped(order):
			continue
		if line.left <= QTY_EPSILON or (view.scope.item_code and line.item_code != view.scope.item_code):
			continue
		found.append((line_key(line, order), line))
	return [line for _key, line in sorted(found, key=lambda pair: pair[0])]


def _litres_available(qty, line):
	return max(0.0, from_ml(floor_ml(qty * factor_of(line))))


def _holders_out(holders):
	return [
		{
			"hold": h.hold,
			"request_id": h.request_id,
			"version": h.version,
			"qty": round(h.qty, 9),
			"qty_litres": h.qty_litres,
		}
		for h in holders
	]


def advance_row(view, adv, uom_factors=None):
	"""An advance as the lookups and suggest_split list it. Quantities in its unit; qty_available
	never below 0."""
	order = view.orders.get(adv.purchase_order) or frappe._dict()
	line = view.lines.get(adv.line) if adv.line else None
	available = max(0.0, adv.available)
	return frappe._dict(
		{
			"name": adv.name,
			"purchase_order": adv.purchase_order,
			"purchase_order_item": adv.line,
			"payment_entry": adv.payment_entry,
			"payment_date": adv.payment_date,
			"advance_paid": adv.advance_paid,
			EXPECTED: adv.expected,
			"qty_consumed": adv.consumed,
			"qty_remaining": adv.remaining,
			"uom_of_item": adv.uom_of_item,
			"conversion_factor": factor_of(line) if line else None,
			"company": order.get("company"),
			"po_status": order.get("status"),
			"skipped": adv.skipped,
			"stuck": adv.stuck,
			"qty_held": adv.held,
			"qty_available": available,
			"qty_available_litres": _litres_available(available, line) if line else 0.0,
			"held_by": _holders_out(adv.held_by),
			"setup_key": adv.setup_key,
			"unit_problem": unit_problem(line, uom_factors) if line else None,
		}
	)


def line_row(view, line, uom_factors=None):
	"""A purchase order line as the lookups and suggest_split list it. Quantities in its unit;
	qty_held counts advance and order hold lines alike; an order line is never stuck."""
	order = view.orders.get(line.purchase_order) or frappe._dict()
	available = max(0.0, line.available)
	return frappe._dict(
		{
			"purchase_order": line.purchase_order,
			"transaction_date": order.get("transaction_date"),
			"schedule_date": order.get("schedule_date"),
			"company": order.get("company"),
			"purchase_order_item": line.name,
			"item_code": line.item_code,
			"uom": line.uom,
			"conversion_factor": factor_of(line),
			"qty": line.qty,
			"received_qty": line.received_qty,
			"qty_to_receive": line.left,
			"stuck": False,
			"qty_held": line.held,
			"qty_available": available,
			"qty_available_litres": _litres_available(available, line),
			"held_by": _holders_out(line.held_by),
			"setup_key": order.get("setup_key"),
			"unit_problem": unit_problem(line, uom_factors),
		}
	)


# ---- checking a split ------------------------------------------------------------------------------
def _resolved(line):
	"""A split line as a hold stores it; the unit fields are filled once its source is known."""
	return frappe._dict(
		line_no=int(line.line_no),
		source_type=line.source_type,
		parc=line.get("parc") or None,
		purchase_order=line.get("purchase_order") or None,
		purchase_order_item=line.get("purchase_order_item") or None,
		uom=None,
		conversion_factor=None,
		qty=None,
		qty_litres=from_ml(to_ml(line.qty_litres)),
	)


def _refusal(line, code, message, available_litres=None, holders=()):
	return {
		"line_no": int(line.line_no),
		"code": code,
		"message": _("Line {0}: {1}").format(line.line_no, message),
		"source_type": line.source_type,
		"parc": line.get("parc") or None,
		"purchase_order": line.get("purchase_order") or None,
		"purchase_order_item": line.get("purchase_order_item") or None,
		"qty_litres": from_ml(to_ml(line.qty_litres)),
		"available_litres": available_litres,
		"held_by": _holders_out(holders),
	}


def _split_refusal(code, message):
	return {
		"line_no": None,
		"code": code,
		"message": message,
		"source_type": None,
		"parc": None,
		"purchase_order": None,
		"purchase_order_item": None,
		"qty_litres": None,
		"available_litres": None,
		"held_by": [],
	}


def _merge_holders(*groups):
	merged = []
	for group in groups:
		for holder in group:
			found = next((h for h in merged if h.hold == holder.hold), None)
			if found is None:
				merged.append(frappe._dict(holder))
			elif found is not holder:
				found.qty = max(found.qty, holder.qty)
				found.qty_litres = max(found.qty_litres, holder.qty_litres)
	return merged


def _short_reason(source, available_litres, asked_litres, holders, earlier_litres):
	reason = _("{0} has {1:.3f} L available; the line asks {2:.3f} L").format(
		source, available_litres, asked_litres
	)
	if holders:
		named = ", ".join(f"{h.request_id} v{h.version}" for h in holders)
		reason += _(" ({0:.3f} L held by {1})").format(sum(h.qty_litres for h in holders), named)
	if earlier_litres > 0.0005:
		reason += _("; earlier lines of this split take {0:.3f} L of its order line").format(earlier_litres)
	return reason


def _units_of(out, line):
	factor = factor_of(line)
	out.purchase_order = line.purchase_order
	out.purchase_order_item = line.name
	out.uom = line.uom
	out.conversion_factor = factor
	out.qty = flt(out.qty_litres / factor, 9)


def _parc_line(view, line, taken, seen, uom_factors, unit):
	scope = view.scope
	out = _resolved(line)
	if ("PARC", line.parc) in seen:
		return _refusal(line, DUPLICATE_SOURCE, _("advance {0} is on more than one line of this split").format(line.parc)), out
	seen.add(("PARC", line.parc))
	adv = view.advances.get(line.parc)
	if adv is None:
		return _refusal(line, PARC_NOT_OPEN, _("advance {0} does not exist").format(line.parc)), out
	out.purchase_order = adv.purchase_order
	if adv.docstatus == 1:
		why = (
			_("is used up: Purchase Receipt {0} used the last of it").format(adv.purchase_receipt)
			if adv.purchase_receipt
			else _("is closed")
		)
		return _refusal(line, PARC_NOT_OPEN, _("advance {0} {1}").format(line.parc, why)), out
	if adv.docstatus == 2:
		return _refusal(line, PARC_NOT_OPEN, _("advance {0} is cancelled").format(line.parc)), out
	if adv.payment_docstatus != 1:
		return _refusal(
			line, PARC_NOT_OPEN, _("advance {0}'s payment {1} is not submitted").format(line.parc, adv.payment_entry)
		), out
	order = view.orders.get(adv.purchase_order)
	if not order:
		return _refusal(
			line, PO_NOT_OPEN, _("advance {0}'s Purchase Order {1} does not exist").format(line.parc, adv.purchase_order)
		), out
	if order.supplier != scope.supplier:
		return _refusal(
			line,
			SOURCE_MISMATCH,
			_("advance {0} is an advance to supplier {1}, not {2}").format(line.parc, order.supplier, scope.supplier),
		), out
	if scope.company and order.company != scope.company:
		return _refusal(
			line,
			SOURCE_MISMATCH,
			_("advance {0} is for company {1}, not {2}").format(line.parc, order.company, scope.company),
		), out
	if line.get("purchase_order") and line.purchase_order != adv.purchase_order:
		return _refusal(
			line,
			SOURCE_MISMATCH,
			_("advance {0} is on Purchase Order {1}; the line names {2}").format(
				line.parc, adv.purchase_order, line.purchase_order
			),
		), out
	if order.docstatus != 1:
		return _refusal(
			line, PO_NOT_OPEN, _("advance {0}'s Purchase Order {1} is not submitted").format(line.parc, order.name)
		), out
	if is_skipped(order):
		return _refusal(
			line,
			PARC_SKIPPED,
			_("advance {0} is on Purchase Order {1}, which is {2}: no receipt can use it until the order is re-opened").format(
				line.parc, order.name, order.status
			),
		), out
	order_lines = view.by_order.get(adv.purchase_order, [])
	if line.get("purchase_order_item"):
		po_line = next(
			(
				candidate
				for candidate in order_lines
				if candidate.name == line.purchase_order_item
				and candidate.uom == adv.uom_of_item
				and (not scope.item_code or candidate.item_code == scope.item_code)
			),
			None,
		)
	else:
		po_line = view.lines.get(adv.line) if adv.line else None
	if po_line is None:
		return _refusal(
			line,
			SOURCE_MISMATCH,
			_("advance {0}'s Purchase Order {1} has no line {2}for item {3} in {4}").format(
				line.parc,
				adv.purchase_order,
				f"{line.purchase_order_item} " if line.get("purchase_order_item") else "",
				scope.item_code,
				adv.uom_of_item,
			),
		), out
	_units_of(out, po_line)
	if adv.remaining <= QTY_EPSILON:
		return _refusal(line, PARC_NOT_OPEN, _("advance {0} has nothing left").format(line.parc)), out
	if is_stuck(adv.remaining, po_line, False):
		return _refusal(
			line,
			PARC_STUCK,
			_("advance {0} has {1:.3f} {2} left, but its order line {3} has nothing left to receive").format(
				line.parc, adv.remaining, adv.uom_of_item, po_line.name
			),
		), out
	problem = unit_problem(po_line, uom_factors) or unit_changed(po_line, unit)
	if problem:
		return _refusal(
			line, UOM_FACTOR_MISMATCH, _("Purchase Order {0} line {1} {2}").format(po_line.purchase_order, po_line.name, problem)
		), out
	earlier = taken.get(po_line.name, 0.0)
	room = min(adv.remaining - adv.held, po_line.available - earlier)
	need = out.qty
	taken[po_line.name] = earlier + max(0.0, min(need, room))
	if need - room > QTY_EPSILON:
		holders = _merge_holders(adv.held_by, po_line.held_by)
		available_litres = _litres_available(room, po_line)
		return _refusal(
			line,
			SHORT,
			_short_reason(
				_("advance {0}").format(line.parc),
				available_litres,
				out.qty_litres,
				holders,
				earlier * factor_of(po_line),
			),
			available_litres,
			holders,
		), out
	return None, out


def _po_line(view, line, taken, seen, uom_factors, unit):
	scope = view.scope
	out = _resolved(line)
	order = view.orders.get(line.purchase_order)
	if not order:
		return _refusal(line, PO_NOT_OPEN, _("Purchase Order {0} does not exist").format(line.purchase_order)), out
	if order.supplier != scope.supplier:
		return _refusal(
			line,
			SOURCE_MISMATCH,
			_("Purchase Order {0} is for supplier {1}, not {2}").format(order.name, order.supplier, scope.supplier),
		), out
	if scope.company and order.company != scope.company:
		return _refusal(
			line,
			SOURCE_MISMATCH,
			_("Purchase Order {0} is for company {1}, not {2}").format(order.name, order.company, scope.company),
		), out
	if order.docstatus != 1:
		return _refusal(line, PO_NOT_OPEN, _("Purchase Order {0} is not submitted").format(order.name)), out
	if is_skipped(order):
		return _refusal(
			line, PO_NOT_OPEN, _("Purchase Order {0} is {1}: ERP takes no receipt against it").format(order.name, order.status)
		), out
	po_line = order_line(view.by_order.get(order.name, []), scope.item_code, line.get("purchase_order_item"))
	if po_line is None:
		return _refusal(
			line,
			SOURCE_MISMATCH,
			_("Purchase Order {0} has no line {1}for item {2}").format(
				order.name,
				f"{line.purchase_order_item} " if line.get("purchase_order_item") else "",
				scope.item_code,
			),
		), out
	if ("PO", po_line.name) in seen:
		return _refusal(
			line, DUPLICATE_SOURCE, _("Purchase Order line {0} is on more than one line of this split").format(po_line.name)
		), out
	seen.add(("PO", po_line.name))
	_units_of(out, po_line)
	problem = unit_problem(po_line, uom_factors) or unit_changed(po_line, unit)
	if problem:
		return _refusal(
			line, UOM_FACTOR_MISMATCH, _("Purchase Order {0} line {1} {2}").format(order.name, po_line.name, problem)
		), out
	earlier = taken.get(po_line.name, 0.0)
	room = po_line.available - earlier
	need = out.qty
	taken[po_line.name] = earlier + max(0.0, min(need, room))
	if need - room > QTY_EPSILON:
		available_litres = _litres_available(room, po_line)
		return _refusal(
			line,
			SHORT,
			_short_reason(
				_("Purchase Order {0} line {1}").format(order.name, po_line.name),
				available_litres,
				out.qty_litres,
				po_line.held_by,
				earlier * factor_of(po_line),
			),
			available_litres,
			po_line.held_by,
		), out
	return None, out


def check_lines(view, lines, uom_factors=None, units=None):
	"""Every line of a split checked against `view`, in line order: its source is open, in scope and
	converts as FuelBuddy does (`uom_factors`) or as when the hold was placed (`units`: {line_no: (uom,
	conversion_factor)}), and it asks no more than is available net of holds, after the earlier lines
	of the split that book on the same purchase order line. The order of the lines is not checked:
	any order, any amounts.

	`lines`: [{line_no, source_type (PARC or PO), parc, purchase_order, purchase_order_item,
	qty_litres}]. Returns (refusals, resolved): one refusal at most per line, and every line with the
	purchase order line, unit, factor and quantity (in the unit) it books."""
	refusals, resolved, taken, seen = [], [], {}, set()
	units = units or {}
	for line in sorted(lines, key=lambda line: int(line.line_no)):
		check = _parc_line if line.source_type == PARC_LINE else _po_line
		refusal, out = check(view, line, taken, seen, uom_factors, units.get(int(line.line_no)))
		if refusal:
			refusals.append(refusal)
		resolved.append(out)
	return refusals, resolved


def check_split(view, lines, qty_litres, uom_factors=None):
	"""``check_lines``, plus the split as a whole: the lines add up to the receipt (`qty_litres`, at 3
	decimals), and every Purchase Order they book on shares one setup. Returns (refusals, resolved)."""
	refusals, resolved = check_lines(view, lines, uom_factors)
	total = sum(to_ml(line.qty_litres) for line in lines)
	if total != to_ml(qty_litres):
		refusals.append(
			_split_refusal(
				TOTAL_MISMATCH,
				_("The lines add up to {0:.3f} L; the receipt is {1:.3f} L").format(
					from_ml(total), from_ml(to_ml(qty_litres))
				),
			)
		)
	first = None
	for out in resolved:
		order = view.orders.get(out.purchase_order)
		if not order:
			continue
		if first is None:
			first = order
		elif order.setup_key != first.setup_key:
			refusals.append(
				_split_refusal(
					SETUP_MISMATCH,
					_(
						"Purchase Orders {0} and {1} have a different company, price list or tax setup; one "
						"receipt carries one, so Purchase has to align the orders"
					).format(first.name, order.name),
				)
			)
			break
	return refusals, resolved


# ---- the pre-fill ----------------------------------------------------------------------------------
def prefill(view, qty_litres, uom_factors=None):
	"""The oldest-first split for a receipt of `qty_litres`: advances oldest first, each taking all it
	can (what is left of the receipt, or what it has available, whichever is less), then purchase order
	lines oldest order first, the same way. Skipped and stuck advances, and lines whose unit does not
	convert as FuelBuddy's (`uom_factors`), are passed over; so is any source whose order's setup
	differs from the first source taken. Litres are rounded down to whole millilitres, so every line
	stays inside what is available.

	Returns a _dict: lines [{line_no, source_type, parc, purchase_order, purchase_order_item, uom,
	conversion_factor, qty, qty_litres}], total_litres, short_litres (what no source could take) and
	setup_key."""
	need = to_ml(qty_litres)
	taken, lines = {}, []
	setup = None

	def take(source_type, parc, po_line, room, key):
		nonlocal need, setup
		if need <= 0 or room <= QTY_EPSILON or (setup is not None and key != setup):
			return
		litres = min(need, floor_ml(room * factor_of(po_line)))
		if litres <= 0:
			return
		factor = factor_of(po_line)
		qty = flt(from_ml(litres) / factor, 9)
		lines.append(
			frappe._dict(
				line_no=len(lines) + 1,
				source_type=source_type,
				parc=parc,
				purchase_order=po_line.purchase_order,
				purchase_order_item=po_line.name,
				uom=po_line.uom,
				conversion_factor=factor,
				qty=qty,
				qty_litres=from_ml(litres),
			)
		)
		taken[po_line.name] = taken.get(po_line.name, 0.0) + qty
		need -= litres
		setup = key if setup is None else setup

	for adv in open_advances(view):
		if adv.skipped or adv.stuck or not adv.line:
			continue
		po_line = view.lines[adv.line]
		if unit_problem(po_line, uom_factors):
			continue
		room = min(adv.remaining - adv.held, po_line.available - taken.get(po_line.name, 0.0))
		take(PARC_LINE, adv.name, po_line, room, adv.setup_key)
	for po_line in open_lines(view):
		if unit_problem(po_line, uom_factors):
			continue
		order = view.orders.get(po_line.purchase_order)
		take(PO_LINE, None, po_line, po_line.available - taken.get(po_line.name, 0.0), order.setup_key)
	total = to_ml(qty_litres)
	return frappe._dict(
		lines=lines,
		total_litres=from_ml(total - need),
		short_litres=from_ml(need),
		setup_key=setup,
	)


def split_signature(lines):
	"""What makes two splits the same: each line's source and litres, in line order."""
	return [
		(
			line.source_type,
			line.parc if line.source_type == PARC_LINE else line.purchase_order_item,
			to_ml(line.qty_litres),
		)
		for line in sorted(lines, key=lambda line: int(line.line_no))
	]


def same_split(resolved, plan_lines):
	return split_signature(resolved) == split_signature(plan_lines)


# ---- reading ---------------------------------------------------------------------------------------
def _distinct(values):
	return list(dict.fromkeys(value for value in values if value))


def _lock(for_update, mode="for update"):
	return f" {mode}" if for_update else ""


def read_sources(
	supplier, company=None, item_code=None, parcs=(), purchase_orders=(), discover=True, for_update=False
):
	"""What ``build_view`` needs for a scope (supplier, company, item), as a _dict: scope, orders,
	lines, advances and queue.

	With `discover`, the supplier's open advances (draft PARCs on submitted payments, on its orders in
	the company, for the item) and the orders with open lines for the item; plus the advances in
	`parcs` and the orders in `purchase_orders` whatever their state, so that a split naming them can
	be told why not. For every order: its lines (for the item, in an item scope). With `for_update`,
	read under the locks of steps 1 to 3 (module docstring)."""
	scope = frappe._dict(supplier=supplier, company=company or None, item_code=item_code or None)
	open_parcs = _open_advance_names(scope) if discover else []
	heads = _advance_heads(_distinct(list(open_parcs) + list(parcs)))
	order_names = sorted(
		_distinct(
			list(purchase_orders)
			+ [head.purchase_order for head in heads.values()]
			+ (_open_line_orders(scope) if discover else [])
		)
	)
	lines = read_lines(_line_names(order_names, scope.item_code), for_update)  # 1
	orders = _read_orders(order_names, for_update)  # 2
	taxes = _read_taxes(order_names)  # a submitted order's taxes cannot change
	for order in orders.values():
		order.setup_key = setup_key(order, taxes.get(order.name, ()))
	queue = sorted(heads.values(), key=queue_key)
	return frappe._dict(
		scope=scope,
		orders=orders,
		lines=lines,
		advances=_read_advances(queue, for_update),  # 3
		queue=[head.name for head in queue],
	)


def _open_advance_names(scope):
	conditions = ["parc.docstatus = 0", "pe.docstatus = 1", "po.supplier = %(supplier)s"]
	if scope.company:
		conditions.append("po.company = %(company)s")
	if scope.item_code:
		conditions.append(
			"exists (select 1 from `tabPurchase Order Item` poi where poi.parent = po.name "
			"and poi.parenttype = 'Purchase Order' and poi.item_code = %(item_code)s)"
		)
	return frappe.db.sql_list(
		f"""select parc.name from `tab{PARC}` parc
		join `tabPurchase Order` po on po.name = parc.purchase_order
		join `tabPayment Entry` pe on pe.name = parc.payment_entry
		where {" and ".join(conditions)}""",
		dict(scope),
	)


def _open_line_orders(scope):
	conditions = [
		"po.docstatus = 1",
		"po.supplier = %(supplier)s",
		"po.status not in %(skip)s",
		"poi.qty - poi.received_qty > %(epsilon)s",
	]
	if scope.company:
		conditions.append("po.company = %(company)s")
	if scope.item_code:
		conditions.append("poi.item_code = %(item_code)s")
	return frappe.db.sql_list(
		f"""select distinct po.name from `tabPurchase Order` po
		join `tabPurchase Order Item` poi on poi.parent = po.name and poi.parenttype = 'Purchase Order'
		where {" and ".join(conditions)}""",
		{**scope, "skip": PO_STATUSES_TAKING_NO_RECEIPT, "epsilon": QTY_EPSILON},
	)


def _advance_heads(names):
	"""{name: head}: an advance's order and payment, which never change. A plain read: it only decides
	the order the advances are locked in."""
	if not names:
		return {}
	rows = frappe.db.sql(
		f"""select parc.name, parc.purchase_order, parc.payment_entry,
			pe.posting_date as payment_date, pe.creation as payment_created, pe.docstatus as payment_docstatus
		from `tab{PARC}` parc left join `tabPayment Entry` pe on pe.name = parc.payment_entry
		where parc.name in %(names)s""",
		{"names": tuple(names)},
		as_dict=True,
	)
	return {row.name: row for row in rows}


def queue_order(names):
	"""`names` (advances) in the order every path locks them: oldest first, unknown names last."""
	heads = _advance_heads(_distinct(names))
	known = [head.name for head in sorted(heads.values(), key=queue_key)]
	return known + sorted(name for name in _distinct(names) if name not in heads)


def _line_names(order_names, item_code=None):
	if not order_names:
		return []
	condition = " and item_code = %(item_code)s" if item_code else ""
	return frappe.db.sql_list(
		f"""select name from `tabPurchase Order Item`
		where parenttype = 'Purchase Order' and parent in %(orders)s{condition}""",
		{"orders": tuple(order_names), "item_code": item_code},
	)


def read_lines(names, for_update=False):
	"""{name: line} for purchase order lines; with `for_update`, locked, by name (step 1)."""
	names = sorted(_distinct(names))
	if not names:
		return {}
	rows = frappe.db.sql(
		f"""select name, parent as purchase_order, idx, item_code, uom, stock_uom, conversion_factor,
			qty, received_qty
		from `tabPurchase Order Item` where name in %(names)s order by name{_lock(for_update)}""",
		{"names": tuple(names)},
		as_dict=True,
	)
	return {row.name: row for row in rows}


def _read_orders(names, for_update):
	if not names:
		return {}
	rows = frappe.db.sql(
		f"""select name, supplier, company, status, docstatus, transaction_date, schedule_date, creation,
			buying_price_list, tax_category, taxes_and_charges
		from `tabPurchase Order` where name in %(names)s
		order by name{_lock(for_update, "lock in share mode")}""",
		{"names": tuple(names)},
		as_dict=True,
	)
	return {row.name: row for row in rows}


def _read_taxes(names):
	taxes = {}
	if not names:
		return taxes
	for row in frappe.db.sql(
		"""select parent, account_head, charge_type, rate, add_deduct_tax
		from `tabPurchase Taxes and Charges`
		where parenttype = 'Purchase Order' and parentfield = 'taxes' and parent in %(names)s
		order by parent, idx""",
		{"names": tuple(names)},
		as_dict=True,
	):
		taxes.setdefault(row.parent, []).append(row)
	return taxes


def _read_advances(queue, for_update):
	"""{name: advance} for the heads in `queue`, read one by one in that order (with `for_update`,
	locked: step 3), with what their active consumption rows book."""
	advances = {}
	for head in queue:
		rows = frappe.db.sql(
			f"""select name, docstatus, purchase_order, purchase_receipt, uom_of_item, advance_paid,
				`{EXPECTED}` as expected
			from `tab{PARC}` where name = %s{_lock(for_update)}""",
			(head.name,),
			as_dict=True,
		)
		if rows:
			advances[head.name] = frappe._dict(head, **rows[0])
	used = _active_consumption(list(advances), for_update)
	for name, adv in advances.items():
		adv.consumed = used.get(name, 0.0)
	return advances


def _active_consumption(names, for_update):
	if not names:
		return {}
	rows = frappe.db.sql(
		f"""select parent, sum(qty) as qty from `tab{CONSUMPTION}`
		where parenttype = %(parenttype)s and parent in %(names)s and is_active = 1
		group by parent{_lock(for_update, "lock in share mode")}""",
		{"parenttype": PARC, "names": tuple(names)},
		as_dict=True,
	)
	return {row.parent: flt(row.qty) for row in rows}


def held_lines(lines=(), parcs=(), exclude_request=None, exclude_hold=None, for_update=False):
	"""Live hold lines (of holds in status Held) on these purchase order lines or advances, one query
	per column so each uses its own index. With `for_update`, LOCK IN SHARE MODE (step 5)."""
	found = {}
	for column, values in (("purchase_order_item", lines), ("parc", parcs)):
		values = _distinct(values)
		if not values:
			continue
		conditions = [f"l.{column} in %(values)s", "l.parenttype = %(parenttype)s", "h.status = %(held)s"]
		if exclude_request:
			conditions.append("h.request_id != %(exclude_request)s")
		if exclude_hold:
			conditions.append("h.name != %(exclude_hold)s")
		for row in frappe.db.sql(
			f"""select l.name, h.name as hold, h.request_id, h.version, l.source_type, l.parc,
				l.purchase_order_item, l.qty, l.qty_litres
			from `tab{HOLD_LINE}` l join `tab{HOLD}` h on h.name = l.parent
			where {" and ".join(conditions)}
			order by l.name{_lock(for_update, "lock in share mode")}""",
			{
				"values": tuple(values),
				"parenttype": HOLD,
				"held": HELD,
				"exclude_request": exclude_request,
				"exclude_hold": exclude_hold,
			},
			as_dict=True,
		):
			found[row.name] = row
	return list(found.values())


def read_held(sources, exclude_request=None, exclude_hold=None, for_update=False):
	"""Live hold lines on every line and advance in `sources`."""
	return held_lines(
		lines=list(sources.lines),
		parcs=list(sources.advances),
		exclude_request=exclude_request,
		exclude_hold=exclude_hold,
		for_update=for_update,
	)


def holders_by_parc(names, for_update=False):
	"""{advance: [holder]}: who holds what on these advances (qty in the advance's unit)."""
	holders = {}
	for row in held_lines(parcs=names, for_update=for_update):
		if row.source_type == PARC_LINE and row.parc:
			_add_holder(holders.setdefault(row.parc, []), row)
	return holders


def holders_by_line(names, for_update=False):
	"""{purchase order line: [holder]}: who holds what on these lines, advance and order lines alike."""
	holders = {}
	for row in held_lines(lines=names, for_update=for_update):
		_add_holder(holders.setdefault(row.purchase_order_item, []), row)
	return holders


def snapshot(supplier, company=None, item_code=None, for_request=None):
	"""A view of the scope read without locks, leaving out `for_request`'s own live hold."""
	sources = read_sources(supplier, company, item_code)
	return build_view(sources, read_held(sources, exclude_request=for_request))


def advance_states():
	"""Every open advance (a draft PARC on a submitted payment), with its supplier, company, order
	line, remaining and stuck, read without locks: what the daily stuck check needs. No item scope:
	an advance books against its order's line in its unit."""
	rows = frappe.db.sql(
		f"""select parc.name, parc.purchase_order, parc.uom_of_item, parc.`{EXPECTED}` as expected,
			po.supplier, po.company, po.status as po_status
		from `tab{PARC}` parc
		join `tabPurchase Order` po on po.name = parc.purchase_order
		join `tabPayment Entry` pe on pe.name = parc.payment_entry
		where parc.docstatus = 0 and pe.docstatus = 1
		order by parc.name""",
		as_dict=True,
	)
	used = _active_consumption([row.name for row in rows], False)
	lines = read_lines(_line_names(sorted({row.purchase_order for row in rows})), False)
	by_order = {}
	for line in sorted(lines.values(), key=lambda line: (line.purchase_order, int(line.idx or 0))):
		line.left = flt(line.qty) - flt(line.received_qty)
		by_order.setdefault(line.purchase_order, []).append(line)
	for row in rows:
		row.remaining = flt(row.expected) - used.get(row.name, 0.0)
		line = advance_line(by_order.get(row.purchase_order, []), row.uom_of_item)
		row.line = line.name if line else None
		row.left = line.left if line else None
		row.stuck = is_stuck(row.remaining, line, row.po_status in PO_STATUSES_TAKING_NO_RECEIPT)
	return rows
