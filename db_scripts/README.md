### DB script equivalents

Pasteable **Server Script** bodies for a site that cannot take the app yet. They cover the Payment
Entry side and the receipt cancel only. A receipt uses an advance by naming it on a row, and that
needs the app: the row field (Purchase Receipt Item `custom_parc`), the consumption table on PARC
(Purchase Advance Consumption), the checks on save and submit, and the booking on submit exist only
there. Bodies are written for the Server Script sandbox: no imports, no `str.format`, no
`_`-prefixed names. Keep either the app **or** these enabled, never both; `after_install` disables
all five script names listed in `install.py`.

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

Without the app an advance is only ever closed whole (by the legacy matcher), so the two cancel
bodies know nothing of partial use:

- The receipt-cancel body re-opens an advance a receipt closed, in full, as a fresh draft.
- The payment-cancel body deletes the payment's draft advances without checking for receipts that
  used part of one. The app refuses that cancel instead.

| Client Script name                                | DocType          | View | Body |
|---------------------------------------------------|------------------|------|------|
| Purchase Advance Receipt Control - PR Dashboard   | Purchase Receipt | Form | `client_scripts/purchase_receipt_parc.js` |

The client script is UI only and needs the app (it reads the two lookups). Before submit it shows
the supplier's open advances, any of which a row may name (one row each), with what each has
available net of receipt split holds and who holds the rest; lists the advances skipped while their
order is Closed or On Hold and the stuck ones (their order line has nothing left to receive); flags
rows that name an advance they cannot use or ask more than is available; and lists the supplier's
open purchase order lines, oldest first, with what each has available. On a receipt that posts a
receipt split hold (the stock lane's) it shows the hold and its status instead. After submit it lists
what each row booked against which advance. It adds a View > PARC button, and shows nothing to users
who cannot read PARC. Nothing is needed on the PARC form itself: every field is read-only.
