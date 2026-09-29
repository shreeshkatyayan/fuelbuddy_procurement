### DB script equivalents

Pasteable **Server Script** bodies for a site that cannot take the app yet. They cover the Payment
Entry side and the receipt cancel only. A receipt uses an advance by naming it on a row, and that
needs the app: the row field (Purchase Receipt Item `custom_parc`), the check on save and the close
on submit exist only there. Bodies are written for the Server Script sandbox: no imports, no
`str.format`, no `_`-prefixed names. Keep either the app **or** these enabled, never both;
`after_install` disables all five script names listed in `install.py`.

| Server Script name (paste as-is)                          | Ref DocType      | Event           | Body |
|-----------------------------------------------------------|------------------|-----------------|------|
| Purchase Advance Receipt Control Payment Entry            | Payment Entry    | After Submit    | `server_scripts/parc_payment_entry_after_submit.py` |
| Purchase Advance Receipt Control Payment Entry Cancel     | Payment Entry    | After Cancel    | `server_scripts/parc_payment_entry_after_cancel.py` |
| Purchase Advance Receipt Control Purchase Receipt Cancel  | Purchase Receipt | After Cancel    | `server_scripts/parc_purchase_receipt_after_cancel.py` |

The first name matches the legacy prod script, so pasting the body over the existing record is an
in-place upgrade. The legacy prod scripts "Purchase Advance Receipt Control -  Warning" (Before
Validate; note the double space) and "Purchase Advance Receipt Control Purchase Receipt" (After
Submit) pick an advance by quantity (nearest within +/-20%). Disable them: nothing replaces them
outside the app.

| Client Script name                                | DocType          | View | Body |
|---------------------------------------------------|------------------|------|------|
| Purchase Advance Receipt Control - PR Dashboard   | Purchase Receipt | Form | `client_scripts/purchase_receipt_parc.js` |

The client script is UI only and needs the app (it reads the open-advances lookup). Before submit it
lists the supplier's open advances on the receipt's Purchase Orders, oldest payment first, and which
rows name them; after submit it links the advances the receipt used; it also adds a View > PARC
button. Nothing is needed on the PARC form itself: every field is read-only.
