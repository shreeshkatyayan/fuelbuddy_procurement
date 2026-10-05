### Fuelbuddy Procurement

Buying-side customisations for FuelBuddy ERPNext. First (and so far only) feature: **PARC**.

Purchase Advance Receipt Control (PARC): one row per supplier advance paid against a
Purchase Order, used up by the Purchase Receipts that name it, oldest advance first.

Code-first port of the customisations that lived in the site DB (custom DocType in the
*Buying* module plus DocType-Event Server Scripts), extracted from a production site.

| Event                      | Handler (doctype controller module)          | What it does |
|----------------------------|----------------------------------------------|--------------|
| Payment Entry on_submit    | `create_parc_on_payment_entry`               | One draft PARC per Purchase Order reference on a supplier payment |
| Payment Entry on_cancel    | `delete_draft_parcs_on_payment_entry_cancel` | Deletes that payment's draft PARCs, or refuses while a receipt uses one |
| Purchase Receipt validate  | `check_named_parcs_on_purchase_receipt`      | Refuses a save that names an advance the receipt cannot use |
| Purchase Receipt on_submit | `consume_named_parcs_on_purchase_receipt`    | Books the row's quantity against the named advance; closes it once used up |
| Purchase Receipt on_cancel | `give_back_parcs_on_purchase_receipt_cancel` | Gives the receipt's quantity back; re-opens an advance it had closed |

### How receipts use an advance

An advance usually pays for several deliveries, and no delivery matches it exactly. So an advance is
used up bit by bit:

- A receipt row names the advance it books against in **Advance (PARC)** (Purchase Receipt Item
  `custom_parc`, created by `install.py` on install and on every migrate). A receipt that names no
  advance books nothing against one.
- Each receipt that books against an advance adds a row to the PARC's **consumptions** table
  (child DocType Purchase Advance Consumption): receipt, receipt row, posting date, quantity,
  active. What the advance has left is its quantity (`qty_to_be_received_against_the_advance_paid`)
  minus the quantity of its active rows. `qty_consumed` and `qty_remaining` are stored on the PARC
  for the form and the list; every check recomputes both from the rows.
- The PARC stays a draft (open) while quantity is left. The receipt that uses the last of it
  (within 0.01) submits it (closed), setting `purchase_receipt` to itself.
- Cancelling a receipt makes its rows inactive, so their quantity is back on the advance. A closed
  advance is re-opened: it is cancelled and a fresh draft copy is inserted (new name, same payment
  and Purchase Order, every row, the cancelled receipt's now inactive).

On every save, and again on submit, a receipt is refused (`ParcRefusedError`, a
`ValidationError`, HTTP 417; one line per row at fault) unless each named advance:

- is open (not used up, closed or cancelled), and an advance to the receipt's supplier;
- is on the row's Purchase Order and counted in the row's unit;
- is booked for some quantity, but no more than it has left (within 0.01). The message says what is
  left and which receipt used it last;
- is the supplier's **oldest advance in line with quantity left** within the receipt's company, and
  the only advance the receipt names, on one row (see "Oldest first" below). An advance whose
  Purchase Order is Closed or On Hold is not in line.

A return cannot name an advance and gives nothing back. A name that does not exist, or a cancelled
PARC, is refused first by Frappe's own link check.

Quantity beyond what the advance has left goes on rows without an advance. Which purchase orders
take it is the approver's choice; `get_open_purchase_orders` lists the supplier's open ones oldest
first as a suggestion, and nothing checks the receipt against it.

### How the caller uses it

The purchase manager maps 100% of a receipt's quantity before approving it: the oldest advance in
line first, then purchase orders, which `get_open_purchase_orders` suggests oldest first. The
Purchase Receipt is created in ERPNext only after that mapping is complete. The calling application
enforces this; this app does not check that a receipt's rows cover its whole quantity, and needs no
rule for it.

### Oldest first

The supplier's advances queue by the Payment Entry's posting date, then the order the payments were
entered (creation), then PARC name. The name only separates the advances of one payment: one
payment split across Purchase Orders, or one payment covering two payment terms of one Purchase
Order, which opens two advances on that order. Under the site's PARC naming rule the name is
creation order, without the rule it is a random hash.

The rule for which advances a receipt may name lives in one function,
`advances_a_receipt_may_name`. Today it returns the oldest advance in line with quantity left, and
only that one: a receipt names at most one advance, and what it cannot book there does not spill
onto the next advance. Returning more advances from that function lets a receipt name up to that
many, one row each, in queue order.

An advance whose Purchase Order is Closed or On Hold is **skipped**: it does not count as an older
advance, so the supplier's next advance in line can be named, and it cannot be named itself (ERPNext
refuses a receipt against such an order anyway; the refusal says the advance is skipped). Once the
order is re-opened or resumed, the advance takes its place in line again by its payment, ahead of
newer advances. The skip is read from the order's status at every check (`skipped_in_queue`);
nothing about it is stored on the advance. The lookup lists a skipped advance in its place with
`skipped` true.

### Two receipts at the same time

Submit re-reads, under a row lock held until the transaction ends, every advance the decision
depends on: the supplier's advances in line ahead of the named one, then the named one, oldest first
so that racing receipts lock in one order. Skipped advances are not locked. Frappe loads the
consumption rows with the same locking read. A locking read sees the latest committed rows, so of
two receipts that both passed the save check, the later one is refused if the earlier one has used
what it needs (the message names that receipt). The receipt's own posting in ERPNext runs before
this step and can still end in a deadlock or lock wait timeout; nothing is saved then, and the
receipt can be sent again.

What the submit's queue cannot see: an advance re-opened, a payment made, or a Purchase Order
closed, held or re-opened, by a transaction that commits while this one runs. The queue and the
orders' statuses are read without locks.

### Design choice: a child table on PARC

The smallest change to the existing model that records each receipt's use:

- **Kept:** one PARC per advance, created by the payment as before; draft means open and submitted
  means closed; re-opening a closed advance by cancelling it and inserting a fresh draft; the
  payment-cancel clean-up; the closing fields (`purchase_receipt`, `qty_of_pr`,
  `grand_total_of_pr`, `qty_left_to_be_received_from_po`).
- **Added:** the consumptions table, two stored quantities, and the checks.

Trade-offs against a separate consumption log DocType:

- *For the child table:* the rows travel with the advance, so the lock on the PARC row also guards
  its rows and the form shows the history; nothing new to permission or list.
- *Against:* a closed PARC is submitted, so its rows cannot change. Giving quantity back to it goes
  through the existing cancel-and-copy re-open, which gives the advance a new name. Receipts that
  named the old name keep it, and the history is split between the cancelled PARC (rows as they were
  when it closed) and its open copy (every row, current state). A log DocType would keep the name
  stable but needs its own locking and an open or closed status that no longer matches the PARC's
  docstatus.
- The receipt column is plain text, not a link: a link to a cancelled receipt would block every
  later save of the advance.

On close, `qty_of_pr` is the quantity all active rows booked, `grand_total_of_pr` is the grand total
of the receipt that closed the advance (not what the advance covers), and
`qty_left_to_be_received_from_po` is `PO qty - received to date` (every submitted receipt on the PO,
this one included).

Cancelling a payment deletes its draft advances. It is refused while a receipt uses one: an active
consumption row, or a draft or submitted receipt row naming it. A closed advance blocks the cancel
through Frappe's own link check.

### Lookups for the approval screen (read-only, GET only)

```
GET /api/method/fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control.get_open_advances?supplier=<Supplier ID>[&company=<Company>]
```

The supplier's open advances with quantity left, oldest first: `name`, `purchase_order`,
`payment_entry`, `payment_date`, `advance_paid`, `qty_to_be_received_against_the_advance_paid`,
`qty_consumed`, `qty_remaining`, `uom_of_item`, `company`, `po_status`, `skipped`. An advance whose
Purchase Order is Closed or On Hold is listed in its place with `skipped` true. The first advance
with `skipped` false is the only one the next receipt may name, for at most its `qty_remaining`.
Needs read permission on PARC.

```
GET /api/method/fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control.get_open_purchase_orders?supplier=<Supplier ID>[&company=<Company>]
```

A suggestion for quantity beyond the advance: the supplier's submitted purchase order lines with
quantity still to receive, oldest order first (order date, entry order, name, line), leaving out
orders that are Closed or On Hold. `purchase_order`, `transaction_date`, `schedule_date`,
`company`, `purchase_order_item`, `item_code`, `uom`, `qty`, `received_qty`, `qty_to_receive`.
Needs read permission on Purchase Order.

### Tests

Both modules sit next to the controller.

- `test_parc_rules.py` needs no bench: what an advance has left, each refusal, oldest first (skipping
  advances on Closed or On Hold orders) and one advance per receipt, the submit deciding on locked
  reads, booking and closing, cancel giving back and re-opening, the payment-cancel guard, the
  lookup's order and skipped flag, the hooks wiring, the DocType JSON and the install fill. It uses
  a stand-in `frappe` when the real one is not installed.

  ```bash
  python -m unittest fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.test_parc_rules
  ```

- `test_parc_named_advances.py` runs the receipt's life on a site with ERPNext: bit-by-bit use and
  closing, overflow, each refusal, oldest first, an advance skipped while its order is Closed or On
  Hold and back in line once the order is re-opened, two advances from one payment on payment terms,
  two receipts for one advance, receipt cancel, payment cancel, returns and both lookups. Each test
  makes a supplier of its own (a copy of the latest submitted Purchase Order's supplier), so the
  site's real advances never queue ahead of the test's, and copies that Purchase Order so
  company-specific mandatory fields come along. Everything is rolled back. Use a lab or staging
  site, never production: the tests create real documents and hold their locks while they run.

  ```bash
  bench --site <site> execute fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.test_parc_named_advances.run
  ```

  `bench execute` exits 0 even when tests fail: read the JSON on the last line. Two receipts
  submitted at the same time are tested on one connection: one test skips the save check so the
  submit's locked re-read must refuse the second receipt. A race across two connections is not
  tested, because a second connection cannot see the suite's uncommitted documents.

### DB script equivalents

`db_scripts/` holds pasteable Server Script bodies for the Payment Entry side and the receipt
cancel (for a site that cannot take the app yet), plus a Purchase Receipt Client Script that shows
the supplier's oldest advance and what it has left on the form. See `db_scripts/README.md` for
names, events and the one-mechanism-at-a-time rule.

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/shantanumishra-FB/fuelbuddy_procurement.git --branch main
bench install-app fuelbuddy_procurement
```

`bench migrate` adopts the existing custom DocType **Purchase Advance Receipt Control** into this
app's module (data untouched) and creates **Purchase Advance Consumption**. The `after_install` hook
creates the Purchase Receipt Item field, disables the legacy Server Scripts so nothing runs twice
(delete them from the DB once the app is live), and fills `qty_consumed` and `qty_remaining` on open
advances. `after_migrate` keeps the field and those quantities in step on sites that already have
the app.

Open advances from before this change have no consumption rows, so each counts as unused and joins
the oldest-first queue at its payment date. Review them before go-live: an old advance left open
will be first in line, unless its order is Closed or On Hold.

Record names (`PARC-26-27-000000001`) come from the site's **Document Naming Rule** (prefix
`PARC.-.FY.-.`, 9 digits). That is data, not part of this app, and keeps working unchanged.

### License

mit
