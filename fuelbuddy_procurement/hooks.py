app_name = "fuelbuddy_procurement"
app_title = "Fuelbuddy Procurement"
app_publisher = "Fuelbuddy"
app_description = "Procurement customisations: Purchase Advance Receipt Control (PARC)"
app_email = "shantanu.mishra@fuelbuddy.in"
app_license = "mit"

after_install = "fuelbuddy_procurement.install.after_install"
# Keeps the Purchase Receipt Item field (install.py) in step on sites that already have the app, and
# fills the advance quantities on PARCs saved before those fields existed.
after_migrate = "fuelbuddy_procurement.install.after_migrate"

_PARC = "fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control"

# Payment Entry submit opens an advance (draft PARC); its cancel drops the drafts it opened.
# A Purchase Receipt books against the advance its row names: checked on every save, booked on
# submit (closing the advance once used up), given back on cancel.
doc_events = {
	"Payment Entry": {
		"on_submit": f"{_PARC}.create_parc_on_payment_entry",
		"on_cancel": f"{_PARC}.delete_draft_parcs_on_payment_entry_cancel",
	},
	"Purchase Receipt": {
		"validate": f"{_PARC}.check_named_parcs_on_purchase_receipt",
		"on_submit": f"{_PARC}.consume_named_parcs_on_purchase_receipt",
		"on_cancel": f"{_PARC}.give_back_parcs_on_purchase_receipt_cancel",
	},
}
