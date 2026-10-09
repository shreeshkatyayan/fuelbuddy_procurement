# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Stuck advances, flagged daily for Purchase (IDEV-3334).

An advance is stuck when it has quantity left but the purchase order line it books against has
nothing left to receive (fuelbuddy_procurement.allocation ``is_stuck``): no receipt and no receipt
split can use it until Purchase sorts out the order or the advance. ``stamp_stuck_advances`` runs
daily (hooks.py scheduler_events) and:

1. stamps PARC ``stuck_since`` with today on every advance that is stuck and not yet stamped, and
   clears it on every advance stamped earlier that is no longer stuck (or no longer open). The PARC
   list filters on it.
2. flags Purchase the way ERP flags the stock lane's teams: one Issue per stuck advance, keyed by
   Issue ``custom_app_issue_key`` = ``parc-stuck-<advance>`` and routed to ``custom_lane_team`` =
   Purchase, of the existing Issue Type "Lane Check Mismatch" (fuelbuddy_crm add_lane_issue_fields
   makes the fields, the type and its SLA). A stuck advance's Issue is raised once, re-opened if it
   was resolved while the advance is still stuck, and resolved once the advance is no longer stuck.
   Where those fields or that type are missing (fuelbuddy_crm's patch not run), it raises nothing
   and writes one Error Log naming the stuck advances instead.

Each Issue write is rolled back to its own savepoint when it fails (and goes to the Error Log), so
one bad Issue never stops the stamps or the other Issues. The job commits when it ends (Frappe's
scheduler).
"""

import frappe
from frappe import _

from fuelbuddy_procurement import allocation
from fuelbuddy_procurement.allocation import PARC

STUCK_SINCE = "stuck_since"
ISSUE_KEY_PREFIX = "parc-stuck-"
# fuelbuddy_crm lane_issue: Issue fields, the team and the Issue Type the lane raises Issues with.
ISSUE_KEY_FIELD = "custom_app_issue_key"
LANE_TEAM_FIELD = "custom_lane_team"
LANE_TEAM = "Purchase"
ISSUE_TYPE = "Lane Check Mismatch"
CLOSED_STATUSES = ("Resolved", "Closed")
SUBJECT_LENGTH = 140
_SAVEPOINT = "fb_parc_stuck_issue"


def stamp_stuck_advances():
	"""Daily: stamp and clear PARC stuck_since, then flag Purchase. See the module docstring."""
	today = frappe.utils.getdate(frappe.utils.nowdate())
	stuck = {state.name: state for state in allocation.advance_states() if state.stuck}
	stamped = {
		row.name: row.get(STUCK_SINCE)
		for row in frappe.get_all(PARC, filters={STUCK_SINCE: ["is", "set"]}, fields=["name", STUCK_SINCE])
	}
	for name, value in stamp_changes(stuck, stamped, today).items():
		frappe.db.set_value(PARC, name, STUCK_SINCE, value, update_modified=False)
	for name, state in stuck.items():
		state.stuck_since = stamped.get(name) or today
	flag_purchase(sorted(stuck.values(), key=lambda state: state.name))


def stamp_changes(stuck, stamped, today):
	"""{advance: new stuck_since}: today for a stuck advance not stamped yet, None for a stamped one
	that is not stuck (any more). `stuck`: names of the stuck advances; `stamped`: {name: stuck_since}
	of the advances stamped now."""
	changes = {name: today for name in sorted(stuck) if not stamped.get(name)}
	changes.update({name: None for name in sorted(stamped) if name not in stuck})
	return changes


def issue_key(advance):
	return f"{ISSUE_KEY_PREFIX}{advance}"


def flag_purchase(stuck):
	"""One open Issue for Purchase per stuck advance in `stuck` (allocation.advance_states rows with
	stuck_since); the Issues of advances no longer stuck are resolved."""
	if not lane_issues_ready():
		if stuck:
			frappe.log_error(
				title=_("Stuck advances: no lane Issue raised"),
				message=_(
					"Issue has no {0} / {1} field, or Issue Type {2} is missing (fuelbuddy_crm "
					"add_lane_issue_fields). Stuck advances: {3}"
				).format(ISSUE_KEY_FIELD, LANE_TEAM_FIELD, ISSUE_TYPE, ", ".join(state.name for state in stuck)),
			)
		return
	for state in stuck:
		_in_savepoint(lambda: open_issue(state), state.name)
	still_stuck = {issue_key(state.name) for state in stuck}
	for row in frappe.get_all(
		"Issue",
		filters={ISSUE_KEY_FIELD: ["like", f"{ISSUE_KEY_PREFIX}%"], "status": ["not in", CLOSED_STATUSES]},
		fields=["name", ISSUE_KEY_FIELD],
	):
		if row.get(ISSUE_KEY_FIELD) not in still_stuck:
			_in_savepoint(lambda: _set_status(row.name, "Resolved"), row.name)


def lane_issues_ready():
	meta = frappe.get_meta("Issue")
	return bool(
		meta.has_field(ISSUE_KEY_FIELD)
		and meta.has_field(LANE_TEAM_FIELD)
		and frappe.db.exists("Issue Type", ISSUE_TYPE)
	)


def open_issue(state):
	"""The advance's Issue: raised when there is none, re-opened when it was resolved or closed."""
	found = frappe.db.get_value("Issue", {ISSUE_KEY_FIELD: issue_key(state.name)}, ["name", "status"], as_dict=True)
	if found:
		if found.status in CLOSED_STATUSES:
			_set_status(found.name, "Open")
		return found.name
	issue = frappe.get_doc(
		{
			"doctype": "Issue",
			"subject": issue_subject(state),
			"description": issue_description(state),
			"issue_type": ISSUE_TYPE,
			ISSUE_KEY_FIELD: issue_key(state.name),
			LANE_TEAM_FIELD: LANE_TEAM,
		}
	).insert(ignore_permissions=True)
	return issue.name


def issue_subject(state):
	"""Carries the advance's name; never translated, so the Issue reads the same for everyone."""
	return f"Advance {state.name} is stuck: its Purchase Order line has nothing left to receive"[:SUBJECT_LENGTH]


def issue_description(state):
	lines = [
		_("Advance (PARC) {0} has {1:.3f} {2} left, but the line of Purchase Order {3} it books against ({4}) has nothing left to receive, so no receipt can use it.").format(
			state.name, state.remaining, state.uom_of_item, state.purchase_order, state.line
		),
		_("Supplier: {0}. Company: {1}.").format(state.supplier, state.company),
		_("Stuck since {0}. A receipt split cannot name it while it is stuck.").format(state.stuck_since),
		_("This Issue is resolved when the advance is no longer stuck."),
	]
	return "<br>".join(frappe.utils.escape_html(line) for line in lines)


def _set_status(name, status):
	issue = frappe.get_doc("Issue", name)
	issue.status = status
	issue.save(ignore_permissions=True)


def _in_savepoint(step, label):
	frappe.db.savepoint(_SAVEPOINT)
	try:
		step()
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		frappe.log_error(title=_("Stuck advances: Issue for {0} not written").format(label))
	else:
		frappe.db.release_savepoint(_SAVEPOINT)
