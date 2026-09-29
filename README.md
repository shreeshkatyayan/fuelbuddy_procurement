### Fuelbuddy Procurement

Buying-side customisations for FuelBuddy ERPNext. First (and so far only) feature: **PARC**.

Purchase Advance Receipt Control (PARC): one row per supplier advance paid against a
Purchase Order, closed by the Purchase Receipt that names it.

Code-first port of the customisations that lived in the site DB (custom DocType in the
*Buying* module plus DocType-Event Server Scripts). Extracted from the prod restore on
2026-09-22.

| Event                      | Handler (doctype controller module)          | What it does |
|----------------------------|----------------------------------------------|--------------|
| Payment Entry on_submit    | `create_parc_on_payment_entry`               | One draft PARC per Purchase Order reference on a supplier payment |
| Payment Entry on_cancel    | `delete_draft_parcs_on_payment_entry_cancel` | Deletes that payment's draft PARCs |
| Purchase Receipt validate  | `check_named_parcs_on_purchase_receipt`      | Refuses a save that names an advance the receipt cannot use |
| Purchase Receipt on_submit | `close_named_parcs_on_purchase_receipt`      | Closes exactly the advances the rows name, or refuses the receipt |
| Purchase Receipt on_cancel | `reopen_parc_on_purchase_receipt_cancel`     | Cancels the PARCs the receipt closed and re-opens each as a fresh draft |

### How a receipt uses an advance

- A receipt row names the advance it uses in **Advance (PARC)** (Purchase Receipt Item
  `custom_parc`, created by `install.py` on install and on every migrate). Nothing is matched
  by quantity: a receipt that names no advance closes none.
- An advance is used whole, by one row. On every save, and again on submit, each named advance
  must be open (not used by another receipt, not cancelled), an advance to the receipt's
  supplier, for the row's Purchase Order, counted in the row's unit, and exactly the row's
  quantity (within 0.01). Quantity beyond the advances goes on rows without an advance.
- Oldest-first is not enforced here. A row may name any open advance on its Purchase Order, even
  while an older advance of the same supplier is still open. `get_open_advances` lists them
  oldest payment first for whoever books the receipt to follow.
- Any failure refuses the whole receipt with `ParcRefusedError` (a `ValidationError`, HTTP 417),
  one line per row at fault. A name that does not exist, or a cancelled PARC, is refused first
  by Frappe's own link check.
- Submit re-reads each named PARC under a row lock. A locking read sees the latest committed row,
  so of two receipts naming one advance, the later one is refused as already used, even when both
  passed the save check. The receipt's own posting in ERPNext runs before this and can still end
  in a deadlock or lock wait timeout; the receipt is then not saved and can be sent again.
- A return cannot name an advance, and does not re-open one.
- Cancelling the receipt cancels the PARCs it closed and re-inserts each as a fresh draft (new
  name, same payment and Purchase Order), so the advance is open again.
- Amending a cancelled receipt copies its old advance names (Frappe's Amend copies no-copy fields
  too). Those PARCs are cancelled, so the save is refused until each row names the re-opened
  advance.
- A payment whose draft PARC is named on a draft receipt cannot be cancelled until that receipt
  stops naming it (Frappe's link check refuses the delete).

On close, `qty_of_pr` is the row's quantity, `grand_total_of_pr` is the receipt's grand total (the
same figure on every advance that receipt closes), and `qty_left_to_be_received_from_po` is
`PO qty - received to date` (every submitted receipt on the PO, this one included); the PARC is
closed by a single `submit()`.

### Open advances for the approval screen

```
GET /api/method/fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control.get_open_advances?supplier=<Supplier ID>
```

The supplier's draft PARCs, oldest payment first (Payment Entry posting date, then the order the
payments were entered, then PARC name): `name`, `purchase_order`, `payment_entry`,
`payment_date`, `advance_paid`, `qty_to_be_received_against_the_advance_paid`, `uom_of_item`.
Read-only, GET only, needs read permission on PARC.

The PARC name only separates the advances of one payment split across Purchase Orders, and has no
business meaning: under the site's PARC naming rule it is creation order, without the rule it is a
random hash. The order is a suggestion; saving or submitting a receipt does not check it.

### Tests

Both modules sit next to the controller.

- `test_parc_rules.py` needs no bench: which row may use which advance, the refusal message,
  what submit closes, the submit refusing what its locked re-read shows used, the hooks wiring
  and the install field. It uses a stand-in `frappe` when the real one is not installed.

  ```bash
  python -m unittest fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.test_parc_rules
  ```

- `test_parc_named_advances.py` runs the receipt's life on a site with ERPNext: named advances
  closing, each refusal, two receipts naming one advance, receipt cancel, payment cancel,
  returns, and the lookup's order. Each test copies the site's latest submitted PO so
  company-specific mandatory fields come along. Everything is rolled back. Use a lab or staging
  site, never production: the tests create real documents and hold their locks while they run.

  ```bash
  bench --site <site> execute fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.test_parc_named_advances.run
  ```

  Two receipts submitted at the same time are tested on one connection: one test skips the save
  check so the submit's locked re-read must refuse the second receipt. A race across two
  connections is not tested, because a second connection cannot see the suite's uncommitted
  documents.

### DB script equivalents

`db_scripts/` holds pasteable Server Script bodies for the Payment Entry side and the receipt
cancel (for a site that cannot take the app yet), plus a Purchase Receipt Client Script that
shows the supplier's open advances on the form. See `db_scripts/README.md` for names, events and
the one-mechanism-at-a-time rule.

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/shantanumishra-FB/fuelbuddy_procurement.git --branch main
bench install-app fuelbuddy_procurement
```

`bench migrate` adopts the existing custom DocType **Purchase Advance Receipt Control** into this
app's module (data untouched). The `after_install` hook creates the Purchase Receipt Item field
and disables the legacy Server Scripts so nothing runs twice; delete them from the DB once the
app is live. `after_migrate` keeps the field in step on sites that already have the app.

Record names (`PARC-26-27-000000001`) come from the site's **Document Naming Rule** (prefix
`PARC.-.FY.-.`, 9 digits). That is data, not part of this app, and keeps working unchanged.

### License

mit
