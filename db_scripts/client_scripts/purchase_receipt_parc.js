// Client Script | Purchase Receipt | Form | "Purchase Advance Receipt Control - PR Dashboard"
// Needs the fuelbuddy_procurement app (the Advance (PARC) row field and the open-advances lookup).
// Before submit: the supplier's open advances on this receipt's Purchase Orders, oldest payment
// first, and which rows name them; submitting uses only the advances the rows name. After submit:
// links the advances this receipt used.
const PARC = "Purchase Advance Receipt Control";
const FIELD = "custom_parc";
const EXP = "qty_to_be_received_against_the_advance_paid";
const OPEN_ADVANCES =
	"fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control.get_open_advances";

function parc_pos(frm) {
	return [...new Set((frm.doc.items || []).map((d) => d.purchase_order).filter(Boolean))];
}

async function show_parcs(frm) {
	frm.dashboard.clear_headline();
	const pos = parc_pos(frm);
	if (!pos.length) return;

	if (frm.doc.docstatus === 1) {
		const used = await frappe.db.get_list(PARC, {
			filters: { purchase_receipt: frm.doc.name, docstatus: 1 },
			fields: ["name", "purchase_order"],
		});
		if (used.length) {
			const links = used.map((p) => `${frappe.utils.get_form_link(PARC, p.name, true)} (${p.purchase_order})`);
			frm.dashboard.set_headline_alert(__("Advances used: {0}", [links.join(", ")]), "green");
		}
		return;
	}
	if (!frm.doc.supplier || frm.doc.is_return) return;

	const r = await frappe.call({ method: OPEN_ADVANCES, args: { supplier: frm.doc.supplier }, type: "GET" });
	const open = r.message || [];
	const rows_by_parc = {};
	(frm.doc.items || []).forEach((d) => {
		if (d[FIELD]) (rows_by_parc[d[FIELD]] = rows_by_parc[d[FIELD]] || []).push(d.idx);
	});
	const lines = pos
		.map((po) => {
			const advances = open
				.filter((p) => p.purchase_order === po)
				.map((p) => {
					const rows = rows_by_parc[p.name];
					return __("{0} covers {1} {2} (paid {3}){4}", [
						frappe.utils.get_form_link(PARC, p.name, true),
						format_number(p[EXP], null, 3),
						p.uom_of_item || "",
						frappe.datetime.str_to_user(p.payment_date),
						rows ? " · " + __("named on row {0}", [rows.join(", ")]) : "",
					]);
				});
			return advances.length ? __("{0}: open advances, oldest payment first: {1}", [po, advances.join(", ")]) : null;
		})
		.filter(Boolean);
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
});

frappe.ui.form.on("Purchase Receipt Item", {
	purchase_order(frm) { show_parcs(frm); },
	custom_parc(frm) { show_parcs(frm); },
	items_remove(frm) { show_parcs(frm); },
});
