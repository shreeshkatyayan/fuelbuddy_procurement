// Client Script | Purchase Receipt | Form | "Purchase Advance Receipt Control - PR Dashboard"
// Needs the fuelbuddy_procurement app (the Advance (PARC) row field and the two lookups).
// Before submit: the supplier's oldest advance with quantity left, which is the only one this
// receipt may name (on one row, for at most what it has left), the advances queued behind it, and
// the supplier's open purchase orders oldest first as a suggestion for the rest of the receipt.
// After submit: the advances the rows named. Shows nothing to users who cannot read PARC or POs.
const PARC = "Purchase Advance Receipt Control";
const FIELD = "custom_parc";
const LOOKUPS =
	"fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control.";
const SUGGESTED_POS = 5;

function parc_pos(frm) {
	return [...new Set((frm.doc.items || []).map((d) => d.purchase_order).filter(Boolean))];
}

function parc_link(name) {
	return frappe.utils.get_form_link(PARC, name, true);
}

function parc_qty(value, uom) {
	return `${format_number(value, null, 3)} ${uom || ""}`.trim();
}

async function parc_lookup(frm, method) {
	const r = await frappe.call({
		method: LOOKUPS + method,
		args: { supplier: frm.doc.supplier, company: frm.doc.company },
		type: "GET",
	});
	return r.message || [];
}

async function show_parcs(frm) {
	frm.dashboard.clear_headline();
	if (!frm.doc.supplier || frm.doc.is_return || !frappe.model.can_read(PARC)) return;
	const named = (frm.doc.items || []).filter((d) => d[FIELD]);

	if (frm.doc.docstatus === 1) {
		if (named.length) {
			const lines = named.map((d) =>
				__("Row {0} booked {1} against advance {2}", [d.idx, parc_qty(d.qty, d.uom), parc_link(d[FIELD])])
			);
			frm.dashboard.set_headline_alert(lines.join("<br>"), "green");
		}
		return;
	}
	if (frm.doc.docstatus !== 0) return;

	const open = await parc_lookup(frm, "get_open_advances");
	const lines = [];
	if (open.length) {
		const [oldest, ...queued] = open;
		lines.push(
			__("Oldest advance with quantity left: {0}, {1} left on {2} (paid {3}{4}). Only this advance may be named, on one row, for at most what it has left.", [
				parc_link(oldest.name),
				parc_qty(oldest.qty_remaining, oldest.uom_of_item),
				oldest.purchase_order,
				frappe.datetime.str_to_user(oldest.payment_date),
				["Closed", "On Hold"].includes(oldest.po_status) ? ", " + __("order {0}", [oldest.po_status]) : "",
			])
		);
		named
			.filter((d) => d[FIELD] !== oldest.name)
			.forEach((d) => lines.push(__("Row {0} names {1}: it will be refused.", [d.idx, d[FIELD]])));
		if (queued.length) {
			const behind = queued.map((p) => `${parc_link(p.name)} (${parc_qty(p.qty_remaining, p.uom_of_item)})`);
			lines.push(__("Queued behind it: {0}", [behind.join(", ")]));
		}
	}
	if (frappe.model.can_read("Purchase Order")) {
		const pos = await parc_lookup(frm, "get_open_purchase_orders");
		if (pos.length) {
			const shown = pos
				.slice(0, SUGGESTED_POS)
				.map((po) => `${po.purchase_order} (${parc_qty(po.qty_to_receive, po.uom)})`);
			lines.push(__("Suggestion for the rest, open purchase orders oldest first: {0}", [shown.join(", ")]));
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
	items_remove(frm) { show_parcs(frm); },
});
