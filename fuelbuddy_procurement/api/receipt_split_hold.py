# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Receipt split holds: ERP's side of approving a fuel receipt's split (IDEV-3334).

When a fuel receipt (GRN) is approved, its litres are split over the supplier's advances (PARC) and
purchase order lines. ``place`` reserves every line of that split until the receipt posts: the hold
is a Receipt Split Hold named ``{request_id}-v{version}``, and while it is Held nothing else (another
split, a desk receipt) can take that quantity. Posting the receipt that names the hold consumes it
(fuelbuddy_procurement.receipt_hold); ``release`` gives it back when the request is closed or the
posting is refused. Called by erp-functions; the app's database mints the versions.

Every method answers

    {ok, code, message, retryable, result, hold, refusals}

ok true    the call was understood and answered; ``result`` says what happened (place, release) or
           carries the answer (status, suggest_split); code null, retryable false.
ok false   the call changed nothing; ``code`` says why:
           ERP_VALIDATION  bad input, or not permitted (needs create on Purchase Receipt)  final
           LOCK_RETRY      a lock wait (8 s), a deadlock, or a racing first write          retry
hold       the hold version the answer is about (HOLD below), or null.
refusals   [REFUSAL] for REJECTED and for a status check, else [].

place(request_id, version, supplier, company, item_code, op_key, qty_litres, lines, uom_factors)  POST

    request_id    the app's request id: 1 to 100 of A-Z a-z 0-9 . _ : - (starting with a letter or
                  digit)
    version       whole number from 1, minted by the app; a higher one is newer
    supplier, company, item_code
                  the receipt's scope; every line must be in it
    op_key        the only Purchase Receipt (custom_app_op_key) that may post the hold
    qty_litres    the receipt's litres (> 0); the lines must add up to it at 3 decimals
    lines         1 to 50 of {line_no (whole number from 1, unique), source_type ("PARC" or "PO"),
                  parc (PARC lines), purchase_order (PO lines; optional on a PARC line, and then it
                  must be the advance's), purchase_order_item (optional: which line of the order),
                  qty_litres (> 0)}
    uom_factors   {ERP unit: FuelBuddy's litres per unit}, e.g. {"Litre": 1, "IG": 4.54609}

    Per version, under locks (fuelbuddy_procurement.allocation, steps 1 to 5):

    same version, same input       a replay: the stored answer (HELD, REJECTED, RELEASED, CONSUMED)
    same version, other input      VERSION_CONFLICT, nothing changed
    same version, tombstone        RELEASED (released before it was placed), nothing held
    the request was posted         CONSUMED (hold: the consumed version), nothing held
    a higher version exists        SUPERSEDED (hold: the highest), nothing changed
    a new version                  first the request's live hold, if any, is Released (REPLACED),
                                   whatever happens next; then every line is checked and the
                                   version is stored Held (HELD) or Rejected (REJECTED, refusals)

    A line is checked for an open source in the scope, a unit that converts as uom_factors says, and
    no more than is available net of every other live hold (after the earlier lines of the split
    on the same order line); the split for its total and one setup. The order of the lines is not
    checked: any order, any amounts. ``fifo`` on the hold records whether the split was the
    oldest-first pre-fill (``suggest_split``) at the time.

release(request_id, up_to_version, reason)  POST

    reason        POSTING_REFUSED, APP_WRITE_FAILED, REQUEST_CLOSED or ORPHAN
    RELEASED          the live version, if it is <= up_to_version, is Released now (hold: it)
    ALREADY_RELEASED  no live version <= up_to_version (hold: the newest version <= up_to_version)
    NOT_RELEASABLE    a version <= up_to_version was posted (hold: it); nothing changed
    When up_to_version is higher than every version seen, a tombstone is stored for it (Released,
    tombstone true), so a late place of it, or of any lower version, holds nothing.

status(request_id, check, uom_factors)  or  status(live=1)  GET

    request_id    result {request_id, live_version, versions: [HOLD], check}; hold: the live version
    check=1       re-tests the live version's lines against ERP now, as its posting will (the hold
                  itself left out of what is held; units against the hold's, and uom_factors when
                  given): check {ok, refusals}, and refusals. Null without a live version.
    live=1        result {live_holds: [{name, request_id, version, created, supplier, company,
                  item_code, total_litres}]}

suggest_split(supplier, company, item_code, qty_litres, for_request, uom_factors)  GET

    result {supplier, company, item_code, qty_litres, for_request, advances: [ADVANCE],
    purchase_order_lines: [LINE], prefill: {lines, total_litres, short_litres, setup_key}}.
    for_request leaves that request's own live hold out of what is held. The pre-fill takes
    advances oldest first, each all it can, then order lines oldest first, passing over skipped
    and stuck advances, units that do not convert as uom_factors says, and sources whose setup
    differs from the first source taken. short_litres is what no source could take.

HOLD      {name, request_id, version, status (Held, Rejected, Released, Consumed), live, tombstone,
          supplier, company, item_code, op_key, total_litres, fifo, purchase_receipt, consumed_at,
          release_reason, released_at, created, refusals, lines: [{line_no, source_type, parc,
          purchase_order, purchase_order_item, uom, conversion_factor, qty, qty_litres}]}
          qty is in the order line's unit (uom), qty_litres = qty x conversion_factor.
REFUSAL   {line_no (null for the split), code, message, source_type, parc, purchase_order,
          purchase_order_item, qty_litres, available_litres, held_by: [{hold, request_id, version,
          qty, qty_litres}]}
          per line: PARC_NOT_OPEN, PARC_SKIPPED, PARC_STUCK, PO_NOT_OPEN, SOURCE_MISMATCH,
          UOM_FACTOR_MISMATCH, SHORT, DUPLICATE_SOURCE; for the split: TOTAL_MISMATCH, SETUP_MISMATCH
ADVANCE   allocation.advance_row; LINE: allocation.line_row
"""

import hashlib
import json
import math
import re

import frappe
from frappe import _

from fuelbuddy_procurement import allocation
from fuelbuddy_procurement.allocation import CONSUMED, HELD, HOLD, PARC_LINE, PO_LINE, REJECTED, RELEASED

# result codes
HELD_NOW = "HELD"
REJECTED_NOW = "REJECTED"
SUPERSEDED = "SUPERSEDED"
VERSION_CONFLICT = "VERSION_CONFLICT"
RELEASED_NOW = "RELEASED"
CONSUMED_NOW = "CONSUMED"
ALREADY_RELEASED = "ALREADY_RELEASED"
NOT_RELEASABLE = "NOT_RELEASABLE"
STATUS_RESULT = {HELD: HELD_NOW, REJECTED: REJECTED_NOW, RELEASED: RELEASED_NOW, CONSUMED: CONSUMED_NOW}

# error codes
ERP_VALIDATION = "ERP_VALIDATION"
LOCK_RETRY = "LOCK_RETRY"
RETRYABLE = frozenset({LOCK_RETRY})

# release reasons: REPLACED is place's own; the others are the callers'.
REPLACED = "REPLACED"
RELEASE_REASONS = ("POSTING_REFUSED", "APP_WRITE_FAILED", "REQUEST_CLOSED", "ORPHAN")

# Each write is one transaction that waits at most this long for a row lock, so a click comes back
# "busy, retry" rather than hanging behind a long desk posting.
LOCK_WAIT_SECONDS = 8
MAX_LINES = 50
# Data fields are varchar(140).
MAX_LENGTH = 140
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")
WHOLE_NUMBER = re.compile(r"[0-9]{1,9}")


class Refusal(Exception):
	"""A refusal: returned to the caller as ``ok: false`` with its code."""

	def __init__(self, code, message):
		super().__init__(message)
		self.code = code
		self.message = message


# ---- place -----------------------------------------------------------------------------------------
@frappe.whitelist(methods=["POST"])
def place(
	request_id=None,
	version=None,
	supplier=None,
	company=None,
	item_code=None,
	op_key=None,
	qty_litres=None,
	lines=None,
	uom_factors=None,
):
	"""Hold version `version` of the request's split. See the module docstring."""
	try:
		request = placement(
			request_id, version, supplier, company, item_code, op_key, qty_litres, lines, uom_factors
		)
		_check_permitted()
	except Refusal as refusal:
		return _refused(refusal)
	return _in_transaction(lambda: _place(request))


def placement(request_id, version, supplier, company, item_code, op_key, qty_litres, lines, uom_factors):
	"""The checked input of a place, with its lines_hash; Refusal ERP_VALIDATION when malformed."""
	request = frappe._dict(
		request_id=_request_id(request_id),
		version=_whole_number(version, "version"),
		supplier=_text(supplier, "supplier"),
		company=_text(company, "company"),
		item_code=_text(item_code, "item_code"),
		op_key=_text(op_key, "op_key"),
		qty_litres=_litres(qty_litres, "qty_litres"),
		lines=_lines(lines),
		uom_factors=_uom_factors(uom_factors, required=True),
	)
	request.lines_hash = lines_hash(request)
	return request


def lines_hash(request):
	"""What a replay of the same version must repeat: the scope, op key, total and lines."""
	canonical = {
		"supplier": request.supplier,
		"company": request.company,
		"item_code": request.item_code,
		"op_key": request.op_key,
		"qty_ml": allocation.to_ml(request.qty_litres),
		"lines": [
			[
				line.line_no,
				line.source_type,
				line.parc,
				line.purchase_order,
				line.purchase_order_item,
				allocation.to_ml(line.qty_litres),
			]
			for line in sorted(request.lines, key=lambda line: line.line_no)
		],
	}
	return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def placement_outcome(rows, version, lines_hash):
	"""What a place of `version` meets among the request's hold rows (name, version, status, tombstone,
	lines_hash): (outcome, row), outcome one of replay, conflict, tombstoned, consumed, superseded,
	new."""
	same = next((row for row in rows if row.version == version), None)
	if same:
		if same.tombstone:
			return "tombstoned", same
		if same.lines_hash != lines_hash:
			return "conflict", same
		return "replay", same
	consumed = next((row for row in rows if row.status == CONSUMED), None)
	if consumed:
		return "consumed", consumed
	highest = max(rows, key=lambda row: row.version, default=None)
	if highest and highest.version > version:
		return "superseded", highest
	return "new", None


def _place(request):
	sources = allocation.read_sources(  # steps 1 to 3
		request.supplier,
		request.company,
		request.item_code,
		parcs=[line.parc for line in request.lines if line.source_type == PARC_LINE],
		purchase_orders=[line.purchase_order for line in request.lines if line.purchase_order],
		for_update=True,
	)
	rows = _lock_request(request.request_id)  # step 4
	outcome, row = placement_outcome(rows, request.version, request.lines_hash)
	if outcome != "new":
		return _settled(outcome, row, request)
	for live in [row for row in rows if row.status == HELD]:
		_release_row(live.name, REPLACED)
	view = allocation.build_view(sources, allocation.read_held(sources, for_update=True))  # step 5
	refusals, resolved = allocation.check_split(view, request.lines, request.qty_litres, request.uom_factors)
	plan = allocation.prefill(view, request.qty_litres, request.uom_factors)
	name = _insert_hold(request, resolved, refusals, allocation.same_split(resolved, plan.lines))
	hold = hold_answer(frappe.get_doc(HOLD, name, for_update=True))
	if refusals:
		return _answer(
			REJECTED_NOW,
			_("Rejected {0}: {1} refusal(s)").format(name, len(refusals)),
			hold,
			refusals,
		)
	return _answer(
		HELD_NOW,
		_("Held {0}: {1} line(s), {2:.3f} L").format(name, len(resolved), request.qty_litres),
		hold,
	)


def _settled(outcome, row, request):
	"""The answer to a place that holds nothing new."""
	hold = hold_answer(frappe.get_doc(HOLD, row.name, for_update=True))
	if outcome == "replay":
		return _answer(
			STATUS_RESULT[row.status],
			_("{0} is already {1}").format(row.name, row.status),
			hold,
			hold["refusals"],
		)
	if outcome == "conflict":
		return _answer(
			VERSION_CONFLICT,
			_("{0} exists with another split; a changed split needs a new version").format(row.name),
			hold,
		)
	if outcome == "tombstoned":
		return _answer(
			RELEASED_NOW,
			_("{0} was released ({1}) before it was placed; nothing is held").format(row.name, hold["release_reason"]),
			hold,
		)
	if outcome == "consumed":
		return _answer(
			CONSUMED_NOW,
			_("Request {0} was posted by Purchase Receipt {1} (hold {2}); nothing is held").format(
				request.request_id, hold["purchase_receipt"], row.name
			),
			hold,
		)
	return _answer(
		SUPERSEDED,
		_("Version {0} of request {1} is superseded by version {2}; nothing changed").format(
			request.version, request.request_id, row.version
		),
		hold,
	)


def _insert_hold(request, resolved, refusals, fifo):
	status = REJECTED if refusals else HELD
	doc = frappe.get_doc(
		{
			"doctype": HOLD,
			"request_id": request.request_id,
			"version": request.version,
			"status": status,
			# Unique: one live hold per request, enforced by the database too.
			"live_request_id": request.request_id if status == HELD else None,
			"supplier": request.supplier,
			"company": request.company,
			"item_code": request.item_code,
			"op_key": request.op_key,
			"total_litres": request.qty_litres,
			"lines_hash": request.lines_hash,
			"fifo": 1 if fifo else 0,
			"refusals": json.dumps(refusals) if refusals else None,
			"lines": [
				{
					"line_no": line.line_no,
					"source_type": line.source_type,
					"parc": line.parc,
					"purchase_order": line.purchase_order,
					"purchase_order_item": line.purchase_order_item,
					"uom": line.uom,
					"conversion_factor": line.conversion_factor,
					"qty": line.qty,
					"qty_litres": line.qty_litres,
				}
				for line in resolved
			],
		}
	)
	# Rejected lines may name what does not exist; the checks above stand in for the link checks.
	doc.insert(ignore_permissions=True, ignore_links=True)
	return doc.name


# ---- release ---------------------------------------------------------------------------------------
@frappe.whitelist(methods=["POST"])
def release(request_id=None, up_to_version=None, reason=None):
	"""Release the request's live hold up to `up_to_version`. See the module docstring."""
	try:
		rid = _request_id(request_id)
		up_to = _whole_number(up_to_version, "up_to_version")
		why = _text(reason, "reason")
		if why not in RELEASE_REASONS:
			raise Refusal(
				ERP_VALIDATION, _("reason must be one of {0}, got {1}").format(", ".join(RELEASE_REASONS), why)
			)
		_check_permitted()
	except Refusal as refusal:
		return _refused(refusal)
	return _in_transaction(lambda: _release(rid, up_to, why))


def release_outcome(rows, up_to):
	"""What a release up to `up_to` does to the request's hold rows: (outcome, row, tombstone), outcome
	one of not_releasable (row: the consumed version), release (row: the live version) or already
	(row: the newest version <= up_to, or None); tombstone: whether up_to is newer than every row."""
	consumed = next((row for row in rows if row.status == CONSUMED and row.version <= up_to), None)
	if consumed:
		return "not_releasable", consumed, False
	tombstone = up_to > max((row.version for row in rows), default=0)
	live = next((row for row in rows if row.status == HELD and row.version <= up_to), None)
	if live:
		return "release", live, tombstone
	newest = max((row for row in rows if row.version <= up_to), key=lambda row: row.version, default=None)
	return "already", newest, tombstone


def _release(request_id, up_to, reason):
	rows = _lock_request(request_id)
	outcome, row, tombstone = release_outcome(rows, up_to)
	if outcome == "not_releasable":
		hold = hold_answer(frappe.get_doc(HOLD, row.name, for_update=True))
		return _answer(
			NOT_RELEASABLE,
			_("{0} was posted by Purchase Receipt {1}; it cannot be released").format(row.name, hold["purchase_receipt"]),
			hold,
		)
	if outcome == "release":
		_release_row(row.name, reason)
	name = row.name if row else None
	if tombstone:
		tomb = _insert_tombstone(request_id, up_to, reason)
		name = name if outcome == "release" else tomb
	hold = hold_answer(frappe.get_doc(HOLD, name, for_update=True)) if name else None
	if outcome == "release":
		return _answer(RELEASED_NOW, _("Released {0} ({1})").format(row.name, reason), hold)
	return _answer(
		ALREADY_RELEASED,
		_("Request {0} has no live hold up to version {1}{2}").format(
			request_id, up_to, _("; version {0} can no longer be placed").format(up_to) if tombstone else ""
		),
		hold,
	)


def _insert_tombstone(request_id, version, reason):
	doc = frappe.get_doc(
		{
			"doctype": HOLD,
			"request_id": request_id,
			"version": version,
			"status": RELEASED,
			"tombstone": 1,
			"release_reason": reason,
			"released_at": frappe.utils.now_datetime(),
		}
	)
	doc.insert(ignore_permissions=True, ignore_links=True)
	return doc.name


def _release_row(name, reason):
	"""Held -> Released. The caller holds the row's lock."""
	doc = frappe.get_doc(HOLD, name, for_update=True)
	doc.status = RELEASED
	doc.live_request_id = None
	doc.release_reason = reason
	doc.released_at = frappe.utils.now_datetime()
	doc.flags.ignore_links = True
	doc.save(ignore_permissions=True)


def _lock_request(request_id):
	"""The request's hold rows, locked (step 4). With none, the locking read takes the gap lock on
	request_id, so two first places of one request take turns (or one deadlocks and is retried)."""
	return frappe.db.sql(
		f"""select name, version, status, tombstone, lines_hash from `tab{HOLD}`
		where request_id = %s order by version for update""",
		(request_id,),
		as_dict=True,
	)


# ---- status ----------------------------------------------------------------------------------------
@frappe.whitelist(methods=["GET"])
def status(request_id=None, check=None, uom_factors=None, live=None):
	"""Every version of a request, or every live hold. See the module docstring."""
	try:
		_check_permitted()
		if _flag(live):
			return _read_only(_live_holds)
		rid = _request_id(request_id)
		factors = _uom_factors(uom_factors, required=False)
	except Refusal as refusal:
		return _refused(refusal)
	return _read_only(lambda: _status(rid, _flag(check), factors))


def _status(request_id, check, uom_factors):
	names = frappe.get_all(HOLD, filters={"request_id": request_id}, pluck="name", order_by="version asc")
	versions = [hold_answer(frappe.get_doc(HOLD, name)) for name in names]
	live = next((hold for hold in versions if hold["live"]), None)
	checked, refusals = None, []
	if check and live:
		refusals = check_hold(live, uom_factors)
		checked = {"ok": not refusals, "refusals": refusals}
	if live:
		message = _("{0} is live").format(live["name"])
		if checked is not None:
			message += _(" and still fits") if checked["ok"] else _(": {0} line(s) no longer fit").format(len(refusals))
	else:
		message = _("Request {0} has no live hold ({1} version(s))").format(request_id, len(versions))
	return _answer(
		{
			"request_id": request_id,
			"live_version": live["version"] if live else None,
			"versions": versions,
			"check": checked,
		},
		message,
		live,
		refusals,
	)


def check_hold(hold, uom_factors=None):
	"""Re-test a live hold's lines against ERP now, as its posting will: [REFUSAL]."""
	lines = [frappe._dict(line) for line in hold["lines"]]
	sources = allocation.read_sources(
		hold["supplier"],
		hold["company"],
		hold["item_code"],
		parcs=[line.parc for line in lines if line.source_type == PARC_LINE],
		purchase_orders=[line.purchase_order for line in lines if line.purchase_order],
		discover=False,
	)
	view = allocation.build_view(sources, allocation.read_held(sources, exclude_hold=hold["name"]))
	units = {line.line_no: (line.uom, line.conversion_factor) for line in lines}
	refusals, _resolved = allocation.check_lines(view, lines, uom_factors, units)
	return refusals


def _live_holds():
	rows = frappe.get_all(
		HOLD,
		filters={"status": HELD},
		fields=["name", "request_id", "version", "creation", "supplier", "company", "item_code", "total_litres"],
		order_by="creation asc",
	)
	holds = [
		{
			"name": row.name,
			"request_id": row.request_id,
			"version": int(row.version or 0),
			"created": _time(row.creation),
			"supplier": row.supplier,
			"company": row.company,
			"item_code": row.item_code,
			"total_litres": round(float(row.total_litres or 0), 3),
		}
		for row in rows
	]
	return _answer({"live_holds": holds}, _("{0} live hold(s)").format(len(holds)))


# ---- suggest_split ---------------------------------------------------------------------------------
@frappe.whitelist(methods=["GET"])
def suggest_split(
	supplier=None, company=None, item_code=None, qty_litres=None, for_request=None, uom_factors=None
):
	"""The supplier's sources net of holds and the oldest-first pre-fill. See the module docstring."""
	try:
		_check_permitted()
		scope = frappe._dict(
			supplier=_text(supplier, "supplier"),
			company=_text(company, "company"),
			item_code=_text(item_code, "item_code"),
			qty_litres=_litres(qty_litres, "qty_litres"),
			for_request=_request_id(for_request) if _given(for_request) else None,
			uom_factors=_uom_factors(uom_factors, required=False),
		)
	except Refusal as refusal:
		return _refused(refusal)
	return _read_only(lambda: _suggest(scope))


def _suggest(scope):
	view = allocation.snapshot(scope.supplier, scope.company, scope.item_code, for_request=scope.for_request)
	plan = allocation.prefill(view, scope.qty_litres, scope.uom_factors)
	result = {
		"supplier": scope.supplier,
		"company": scope.company,
		"item_code": scope.item_code,
		"qty_litres": scope.qty_litres,
		"for_request": scope.for_request,
		"advances": [allocation.advance_row(view, adv, scope.uom_factors) for adv in allocation.open_advances(view)],
		"purchase_order_lines": [
			allocation.line_row(view, line, scope.uom_factors) for line in allocation.open_lines(view)
		],
		"prefill": {
			"lines": [dict(line) for line in plan.lines],
			"total_litres": plan.total_litres,
			"short_litres": plan.short_litres,
			"setup_key": plan.setup_key,
		},
	}
	if plan.short_litres > 0:
		message = _("The pre-fill covers {0:.3f} of {1:.3f} L; {2:.3f} L has no source").format(
			plan.total_litres, scope.qty_litres, plan.short_litres
		)
	else:
		message = _("The pre-fill covers {0:.3f} L in {1} line(s)").format(plan.total_litres, len(plan.lines))
	return _answer(result, message)


# ---- answers ---------------------------------------------------------------------------------------
def hold_answer(doc):
	"""HOLD (module docstring) for a Receipt Split Hold document."""
	return {
		"name": doc.name,
		"request_id": doc.request_id,
		"version": int(doc.version or 0),
		"status": doc.status,
		"live": doc.status == HELD,
		"tombstone": bool(int(doc.tombstone or 0)),
		"supplier": doc.supplier or None,
		"company": doc.company or None,
		"item_code": doc.item_code or None,
		"op_key": doc.op_key or None,
		"total_litres": round(float(doc.total_litres or 0), 3),
		"fifo": bool(int(doc.fifo or 0)),
		"purchase_receipt": doc.purchase_receipt or None,
		"consumed_at": _time(doc.consumed_at),
		"release_reason": doc.release_reason or None,
		"released_at": _time(doc.released_at),
		"created": _time(doc.creation),
		"refusals": json.loads(doc.refusals) if doc.refusals else [],
		"lines": [
			{
				"line_no": int(line.line_no or 0),
				"source_type": line.source_type,
				"parc": line.parc or None,
				"purchase_order": line.purchase_order or None,
				"purchase_order_item": line.purchase_order_item or None,
				"uom": line.uom or None,
				"conversion_factor": float(line.conversion_factor) if line.conversion_factor else None,
				"qty": float(line.qty or 0),
				"qty_litres": round(float(line.qty_litres or 0), 3),
			}
			for line in sorted(doc.get("lines") or [], key=lambda line: int(line.line_no or 0))
		],
	}


def _answer(result, message, hold=None, refusals=()):
	return {
		"ok": True,
		"code": None,
		"message": message,
		"retryable": False,
		"result": result,
		"hold": hold,
		"refusals": list(refusals),
	}


def _refused(refusal):
	frappe.clear_messages()
	return {
		"ok": False,
		"code": refusal.code,
		"message": refusal.message,
		"retryable": refusal.code in RETRYABLE,
		"result": None,
		"hold": None,
		"refusals": [],
	}


def _time(value):
	return str(value) if value else None


# ---- transactions ----------------------------------------------------------------------------------
def _in_transaction(step):
	"""Run one locked read-and-write and commit it, or roll it all back and answer with the refusal.
	The session waits at most LOCK_WAIT_SECONDS for a row lock meanwhile."""
	previous = _set_lock_wait(LOCK_WAIT_SECONDS)
	try:
		result = step()
		frappe.db.commit()
		return result
	except Exception as exc:
		frappe.db.rollback()
		refusal = _as_refusal(exc)
		if refusal is None:
			raise  # infrastructure: an HTTP error, which erp-functions treats as "outcome unknown"
		return _refused(refusal)
	finally:
		_set_lock_wait(previous)


def _read_only(step):
	try:
		return step()
	except Exception as exc:
		frappe.db.rollback()
		refusal = _as_refusal(exc)
		if refusal is None:
			raise
		return _refused(refusal)


def _set_lock_wait(seconds):
	"""Set the session's innodb_lock_wait_timeout; returns the value it had."""
	if seconds is None:
		return None
	previous = frappe.db.sql("select @@session.innodb_lock_wait_timeout")[0][0]
	frappe.db.sql("set session innodb_lock_wait_timeout = %s", (int(seconds),))
	return previous


def _as_refusal(exc):
	"""Map an exception raised inside the transaction to its code; None = not ours to answer."""
	if isinstance(exc, Refusal):
		return exc
	if isinstance(
		exc,
		frappe.QueryDeadlockError
		| frappe.QueryTimeoutError
		| frappe.TimestampMismatchError
		| frappe.DuplicateEntryError
		| frappe.UniqueValidationError,
	):
		# A duplicate is the other first place of this request (or version) committing first: a retry
		# reads its row.
		return Refusal(LOCK_RETRY, _message(exc))
	if _is_lock_error(exc):
		return Refusal(LOCK_RETRY, _message(exc))
	if isinstance(exc, frappe.ValidationError | frappe.PermissionError):
		return Refusal(ERP_VALIDATION, _message(exc))
	return None


def _is_lock_error(exc):
	try:
		return bool(frappe.db.is_deadlocked(exc) or frappe.db.is_timedout(exc))
	except Exception:
		return False


def _message(exc):
	return frappe.utils.strip_html(str(exc) or exc.__class__.__name__)[:500]


def _check_permitted():
	# The hold stands for the receipt it reserves quantity for, so the gate is the receipt's.
	if not frappe.has_permission("Purchase Receipt", "create"):
		raise Refusal(ERP_VALIDATION, _("Not permitted to hold receipt quantities (needs create on Purchase Receipt)"))


# ---- input -----------------------------------------------------------------------------------------
def _given(value):
	return value is not None and str(value).strip() != ""


def _flag(value):
	return _given(value) and str(value).strip().lower() in ("1", "true", "yes")


def _text(value, field, required=True):
	text = str(value).strip() if value is not None else ""
	if not text:
		if required:
			raise Refusal(ERP_VALIDATION, _("{0} is required").format(field))
		return None
	if len(text) > MAX_LENGTH:
		raise Refusal(ERP_VALIDATION, _("{0} must be at most {1} characters").format(field, MAX_LENGTH))
	return text


def _request_id(value):
	text = _text(value, "request_id")
	if not REQUEST_ID.fullmatch(text):
		raise Refusal(
			ERP_VALIDATION,
			_("request_id must be 1 to 100 of A-Z, a-z, 0-9, '.', '_', ':' and '-', starting with a letter or digit"),
		)
	return text


def _whole_number(value, field):
	text = str(value).strip() if value is not None and not isinstance(value, bool) else ""
	if not WHOLE_NUMBER.fullmatch(text) or int(text) < 1:
		raise Refusal(ERP_VALIDATION, _("{0} must be a whole number from 1, got {1!r}").format(field, value))
	return int(text)


def _litres(value, field):
	number = None
	if not isinstance(value, bool):
		try:
			number = float(value)
		except (TypeError, ValueError):
			number = None
	if number is None or not math.isfinite(number) or number > 1e9 or allocation.to_ml(number) <= 0:
		raise Refusal(ERP_VALIDATION, _("{0} must be a number of litres above 0, got {1!r}").format(field, value))
	return allocation.from_ml(allocation.to_ml(number))


def _json(value, field, kind):
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			raise Refusal(ERP_VALIDATION, _("{0} must be JSON").format(field))
	if not isinstance(value, kind):
		raise Refusal(ERP_VALIDATION, _("{0} must be a JSON {1}").format(field, "list" if kind is list else "object"))
	return value


def _lines(value):
	items = _json(value, "lines", list)
	if not items:
		raise Refusal(ERP_VALIDATION, _("lines must name at least one line"))
	if len(items) > MAX_LINES:
		raise Refusal(ERP_VALIDATION, _("lines may name at most {0} lines").format(MAX_LINES))
	lines, numbers = [], set()
	for index, item in enumerate(items, 1):
		where = f"lines[{index}]"
		if not isinstance(item, dict):
			raise Refusal(ERP_VALIDATION, _("{0} must be an object").format(where))
		line_no = _whole_number(item.get("line_no"), f"{where}.line_no")
		if line_no in numbers:
			raise Refusal(ERP_VALIDATION, _("{0}.line_no {1} is used twice").format(where, line_no))
		numbers.add(line_no)
		source_type = item.get("source_type")
		if source_type not in (PARC_LINE, PO_LINE):
			raise Refusal(
				ERP_VALIDATION, _("{0}.source_type must be PARC or PO, got {1!r}").format(where, source_type)
			)
		parc = _text(item.get("parc"), f"{where}.parc", required=source_type == PARC_LINE)
		if source_type == PO_LINE and parc:
			raise Refusal(ERP_VALIDATION, _("{0} is a PO line; it names no advance (parc)").format(where))
		lines.append(
			frappe._dict(
				line_no=line_no,
				source_type=source_type,
				parc=parc,
				purchase_order=_text(item.get("purchase_order"), f"{where}.purchase_order", required=source_type == PO_LINE),
				purchase_order_item=_text(item.get("purchase_order_item"), f"{where}.purchase_order_item", required=False),
				qty_litres=_litres(item.get("qty_litres"), f"{where}.qty_litres"),
			)
		)
	return lines


def _uom_factors(value, required):
	if not _given(value) and not isinstance(value, dict):
		if required:
			raise Refusal(ERP_VALIDATION, _("uom_factors is required"))
		return None
	factors = _json(value, "uom_factors", dict)
	if not factors:
		if required:
			raise Refusal(ERP_VALIDATION, _("uom_factors must name at least one unit"))
		return None
	checked = {}
	for unit, factor in factors.items():
		name = _text(unit, "uom_factors unit")
		number = None
		if not isinstance(factor, bool):
			try:
				number = float(factor)
			except (TypeError, ValueError):
				number = None
		if number is None or not math.isfinite(number) or number <= 0:
			raise Refusal(ERP_VALIDATION, _("uom_factors[{0}] must be a number above 0, got {1!r}").format(name, factor))
		checked[name] = number
	return checked
