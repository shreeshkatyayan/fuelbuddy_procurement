// Client Script | Purchase Receipt | Form | "Purchase Advance Receipt Control - PR Dashboard"
// Needs the fuelbuddy_procurement app (the Advance (PARC) row field, the receipt split hold field and
// the two lookups).
// A receipt that posts a receipt split hold (the stock lane's GRN): the hold, its status, and that the
// rows must be its lines.
// Any other receipt, before submit: the supplier's open advances with what each has available net of
// receipt split holds and who holds the rest, the advances skipped while their order is Closed or On
// Hold, the stuck ones (their order line has nothing left to receive), rows that ask more than is
// available, and the supplier's open purchase order lines oldest first with what each has available.
// After submit: what each row booked against which advance. Shows nothing to users who cannot read
// PARC or purchase orders.
const PARC = "Purchase Advance Receipt Control";
const HOLD = "Receipt Split Hold";
const FIELD = "custom_parc";
const HOLD_FIELD = "custom_receipt_split_hold";
const LOOKUPS =
	"fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control.";
const SUGGESTED_POS = 5;
const EPSILON = 0.01;

function parc_pos(frm) {
	return [...new Set((frm.doc.items || []).map((d) => d.purchase_order).filter(Boolean))];
}

function parc_link(name) {
	return frappe.utils.get_form_link(PARC, name, true);
}

function parc_qty(value, uom) {
	return `${format_number(value, null, 3)} ${uom || ""}`.trim();
}

function parc_holders(held_by) {
	return (held_by || []).map((h) => `${frappe.utils.escape_html(h.request_id)} v${h.version}`).join(", ");
}

function parc_available(source, uom) {
	const text = __("{0} available", [parc_qty(source.qty_available, uom)]);
	if (!(source.qty_held > EPSILON)) return text;
	return __("{0} ({1} held by {2})", [text, parc_qty(source.qty_held, uom), parc_holders(source.held_by)]);
}

async function parc_lookup(frm, method) {
	const r = await frappe.call({
		method: LOOKUPS + method,
		args: { supplier: frm.doc.supplier, company: frm.doc.company },
		type: "GET",
	});
	return r.message || [];
}

function booked_rows(frm) {
	return (frm.doc.items || [])
		.filter((d) => d[FIELD])
		.map((d) => __("Row {0} booked {1} against advance {2}", [d.idx, parc_qty(d.qty, d.uom), parc_link(d[FIELD])]));
}

async function show_hold(frm) {
	const name = frm.doc[HOLD_FIELD];
	const lines = [];
	if (frappe.model.can_read(HOLD)) {
		const r = await frappe.db.get_value(HOLD, name, ["status", "request_id", "version"]);
		const hold = r.message || {};
		lines.push(
			__("This receipt posts receipt split hold {0} (request {1} v{2}, {3}).", [
				frappe.utils.get_form_link(HOLD, name, true),
				frappe.utils.escape_html(hold.request_id || ""),
				hold.version || "",
				hold.status || __("not found"),
			])
		);
	} else {
		lines.push(__("This receipt posts receipt split hold {0}.", [frappe.utils.escape_html(name)]));
	}
	if (frm.doc.docstatus === 0) lines.push(__("Its rows must be the hold's lines; submitting it consumes the hold."));
	if (frm.doc.docstatus === 1) lines.push(...booked_rows(frm));
	frm.dashboard.set_headline_alert(lines.join("<br>"), frm.doc.docstatus === 1 ? "green" : "blue");
}

async function show_parcs(frm) {
	frm.dashboard.clear_headline();
	if (!frm.doc.supplier || frm.doc.is_return) return;
	if (frm.doc[HOLD_FIELD]) return show_hold(frm);
	if (!frappe.model.can_read(PARC)) return;

	if (frm.doc.docstatus === 1) {
		const lines = booked_rows(frm);
		if (lines.length) frm.dashboard.set_headline_alert(lines.join("<br>"), "green");
		return;
	}
	if (frm.doc.docstatus !== 0) return;

	const open = await parc_lookup(frm, "get_open_advances");
	const usable = open.filter((p) => !p.skipped && !p.stuck);
	const skipped = open.filter((p) => p.skipped);
	const stuck = open.filter((p) => p.stuck);
	const lines = [];
	if (usable.length) {
		const listed = usable.map((p) => `${parc_link(p.name)}: ${parc_available(p, p.uom_of_item)} on ${p.purchase_order}`);
		lines.push(__("Open advances, any of which a row may name (one row each): {0}", [listed.join("; ")]));
	}
	if (skipped.length) {
		const passed = skipped.map((p) => `${parc_link(p.name)} (${p.purchase_order} ${p.po_status})`);
		lines.push(__("Skipped until their order is re-opened: {0}", [passed.join(", ")]));
	}
	if (stuck.length) {
		const blocked = stuck.map((p) => `${parc_link(p.name)} (${parc_qty(p.qty_remaining, p.uom_of_item)} left on ${p.purchase_order})`);
		lines.push(__("Stuck, their order line has nothing left to receive: {0}", [blocked.join(", ")]));
	}
	const by_name = Object.fromEntries(open.map((p) => [p.name, p]));
	(frm.doc.items || [])
		.filter((d) => d[FIELD])
		.forEach((d) => {
			const p = by_name[d[FIELD]];
			if (!p || p.skipped || p.stuck) {
				lines.push(__("Row {0} names {1}: it will be refused.", [d.idx, d[FIELD]]));
			} else if (d.qty - p.qty_available > EPSILON) {
				lines.push(__("Row {0} books {1} against {2}, which has {3}: it will be refused.", [
					d.idx,
					parc_qty(d.qty, d.uom),
					d[FIELD],
					parc_available(p, p.uom_of_item),
				]));
			}
		});
	if (frappe.model.can_read("Purchase Order")) {
		const pos = await parc_lookup(frm, "get_open_purchase_orders");
		const by_line = Object.fromEntries(pos.map((po) => [po.purchase_order_item, po]));
		const on_rows = {};
		(frm.doc.items || []).forEach((d) => {
			if (d.purchase_order_item) on_rows[d.purchase_order_item] = (on_rows[d.purchase_order_item] || 0) + (d.received_qty || d.qty || 0);
		});
		Object.entries(on_rows).forEach(([line, qty]) => {
			const po = by_line[line];
			if (po && po.qty_held > EPSILON && qty - po.qty_available > EPSILON) {
				lines.push(__("{0} line {1}: this receipt receives {2}, but it has {3}: it will be refused.", [
					po.purchase_order,
					line,
					parc_qty(qty, po.uom),
					parc_available(po, po.uom),
				]));
			}
		});
		if (pos.length) {
			const shown = pos.slice(0, SUGGESTED_POS).map((po) => `${po.purchase_order} (${parc_available(po, po.uom)})`);
			lines.push(__("Open purchase orders, oldest first: {0}", [shown.join(", ")]));
		}
	}
	if (lines.length) frm.dashboard.set_headline_alert(lines.join("<br>"), "orange");
}

frappe.ui.form.on("Purchase Receipt", {
	refresh(frm) {
		show_parcs(frm);
		if (parc_pos(frm).length) {
			frm.add_custom_button(__("PARC"), () => {
				frappe.set_route("List", PARC, { purchase_order: ["in", parc_pos(frm)] });
			}, __("View"));
		}
	},
	supplier(frm) { show_parcs(frm); },
	company(frm) { show_parcs(frm); },
});

frappe.ui.form.on("Purchase Receipt Item", {
	purchase_order(frm) { show_parcs(frm); },
	custom_parc(frm) { show_parcs(frm); },
	qty(frm) { show_parcs(frm); },
	items_remove(frm) { show_parcs(frm); },
});
