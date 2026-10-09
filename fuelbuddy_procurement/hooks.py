app_name = "fuelbuddy_procurement"
app_title = "Fuelbuddy Procurement"
app_publisher = "Fuelbuddy"
app_description = "Procurement customisations: Purchase Advance Receipt Control (PARC) and receipt split holds"
app_email = "shantanu.mishra@fuelbuddy.in"
app_license = "mit"

after_install = "fuelbuddy_procurement.install.after_install"
# Keeps the Purchase Receipt and Purchase Receipt Item fields (install.py) in step on sites that
# already have the app, and fills the advance quantities on PARCs saved before those fields existed.
after_migrate = "fuelbuddy_procurement.install.after_migrate"

_PARC = "fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control"
_RECEIPT = "fuelbuddy_procurement.receipt_events"

# Payment Entry submit opens an advance (draft PARC); its cancel drops the drafts it opened, or is
# refused while a receipt or a receipt split hold uses one.
# A Purchase Receipt that names a receipt split hold (the stock lane's GRN) posts that hold; any other
# books against the advances its rows name, never taking held quantity: checked on every save, booked
# on submit (closing advances once used up), given back on cancel (receipt_events).
doc_events = {
	"Payment Entry": {
		"on_submit": f"{_PARC}.create_parc_on_payment_entry",
		"on_cancel": f"{_PARC}.delete_draft_parcs_on_payment_entry_cancel",
	},
	"Purchase Receipt": {
		"before_insert": "fuelbuddy_procurement.receipt_hold.clear_hold_on_amend",
		"validate": f"{_RECEIPT}.validate",
		"on_submit": f"{_RECEIPT}.on_submit",
		"on_cancel": f"{_PARC}.give_back_parcs_on_purchase_receipt_cancel",
	},
}

# IDEV-3334: stamp PARC stuck_since and flag Purchase about stuck advances.
scheduler_events = {
	"daily": [
		"fuelbuddy_procurement.stuck_advances.stamp_stuck_advances",
	],
}
