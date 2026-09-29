app_name = "fuelbuddy_procurement"
app_title = "Fuelbuddy Procurement"
app_publisher = "Fuelbuddy"
app_description = "Procurement customisations: Purchase Advance Receipt Control (PARC)"
app_email = "shantanu.mishra@fuelbuddy.in"
app_license = "mit"

after_install = "fuelbuddy_procurement.install.after_install"
# Keeps the Purchase Receipt Item field (install.py) in step on sites that already have the app.
after_migrate = "fuelbuddy_procurement.install.create_parc_fields"

_PARC = "fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control"

# Payment Entry submit opens an advance (draft PARC); its cancel drops the drafts it opened.
# A Purchase Receipt uses only the advances its rows name: checked on every save, closed on
# submit, re-opened on cancel.
doc_events = {
	"Payment Entry": {
		"on_submit": f"{_PARC}.create_parc_on_payment_entry",
		"on_cancel": f"{_PARC}.delete_draft_parcs_on_payment_entry_cancel",
	},
	"Purchase Receipt": {
		"validate": f"{_PARC}.check_named_parcs_on_purchase_receipt",
		"on_submit": f"{_PARC}.close_named_parcs_on_purchase_receipt",
		"on_cancel": f"{_PARC}.reopen_parc_on_purchase_receipt_cancel",
	},
}
