### Fuelbuddy Procurement

Buying-side customisations for FuelBuddy ERPNext:

- **PARC**: Purchase Advance Receipt Control, one row per supplier advance paid against a Purchase
  Order, used up by the Purchase Receipts that name it.
- **Receipt split holds** (IDEV-3334): when a fuel receipt is approved in the app, its split over the
  supplier's advances and purchase order lines is reserved in ERP until the receipt posts.

PARC is a code-first port of the customisations that lived in the site DB (custom DocType in the
*Buying* module plus DocType-Event Server Scripts), extracted from a production site.

| Event                          | Handler                                             | What it does |
|--------------------------------|-----------------------------------------------------|--------------|
| Payment Entry on_submit        | PARC `create_parc_on_payment_entry`                 | One draft PARC per Purchase Order reference on a supplier payment |
| Payment Entry on_cancel        | PARC `delete_draft_parcs_on_payment_entry_cancel`   | Deletes that payment's draft PARCs, or refuses while a receipt or a live hold uses one |
| Purchase Receipt before_insert | `receipt_hold.clear_hold_on_amend`                  | An amendment starts without the hold of the receipt it amends |
| Purchase Receipt validate      | `receipt_events.validate`                           | Hold path or desk path (below): refuses a save that cannot post |
| Purchase Receipt on_submit     | `receipt_events.on_submit`                          | Hold path: consumes the hold. Desk path: books the named advances and checks the order lines |
| Purchase Receipt on_cancel     | PARC `give_back_parcs_on_purchase_receipt_cancel`   | Gives the receipt's quantity back; re-opens an advance it had closed |
| Scheduler, daily               | `stuck_advances.stamp_stuck_advances`               | Stamps PARC `stuck_since` and flags Purchase about stuck advances |

### How receipts use an advance

An advance usually pays for several deliveries, and no delivery matches it exactly. So an advance is
used up bit by bit:

- A receipt row names the advance it books against in **Advance (PARC)** (Purchase Receipt Item
  `custom_parc`, created by `install.py` on install and on every migrate). A receipt that names no
  advance books nothing against one.
- Each receipt that books against an advance adds a row to the PARC's **consumptions** table
  (child DocType Purchase Advance Consumption): receipt, receipt row, posting date, quantity,
  active, and the receipt split hold it came from, if any. What the advance has left is its quantity
  (`qty_to_be_received_against_the_advance_paid`) minus the quantity of its active rows.
  `qty_consumed` and `qty_remaining` are stored on the PARC for the form and the list; every check
  recomputes both from the rows.
- The PARC stays a draft (open) while quantity is left. The receipt that uses the last of it
  (within 0.01) submits it (closed), setting `purchase_receipt` to itself.
- Cancelling a receipt makes its rows inactive, so their quantity is back on the advance. A closed
  advance is re-opened: it is cancelled and a fresh draft copy is inserted (new name, same payment
  and Purchase Order, every row, the cancelled receipt's now inactive).

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

Cancelling a payment deletes its draft advances. It is refused while a receipt uses one (an active
consumption row, or a draft or submitted receipt row naming it) or a live receipt split hold holds
quantity on one. A closed advance blocks the cancel through Frappe's own link check.

### Availability, net of holds

One formula (`fuelbuddy_procurement/allocation.py`) decides what an advance or a purchase order line
can still give, for the hold API, the lookups, desk receipts, the posting of a hold and the daily
check. Quantities are in the order line's unit (also the advance's); litres are that quantity times
the line's conversion factor; two quantities within 0.01 are equal.

```
left(line)        = qty - received_qty
available(line)   = left(line) - what live holds hold on the line (advance and order lines alike)
available(advance)= min(what it has left - what live holds hold on it, available(its line))
```

- An advance books against its order's line in its unit (for the receipt's item): the first such
  line with something left to receive.
- **Skipped**: the advance's order is Closed or On Hold. ERPNext takes no receipt against it.
- **Stuck**: the advance has quantity left but its line has nothing left to receive.
- One receipt carries one company, price list and tax setup (`setup_key`).

### Desk receipts

A receipt that names no hold is a desk receipt. On every save, and again on submit under locks, it
is refused (`ParcRefusedError`, a `ValidationError`, HTTP 417; one line per row at fault) unless:

- each named advance is open, an advance to the receipt's supplier and company, on the row's
  Purchase Order and counted in the row's unit, not skipped, named on one row only, and booked for
  some quantity but no more than it has **available** (what it has left less what holds hold on it);
- each purchase order line that holds hold quantity on receives no more than what it has left less
  what is held: **a desk receipt never takes held quantity** (with no holds this checks nothing);
- with the site flag `fuelbuddy_procurement_desk_rules` on, every purchase order line receives no
  more than what it has left less what is held: a hard cap, so the over-receipt allowance no longer
  applies to purchase receipts.

A receipt may name any open advances of its supplier, in any order, one row each (IDEV-3334 retired
the oldest-first, one-advance rule). A return cannot name an advance and gives nothing back. A name
that does not exist, or a cancelled PARC, is refused first by Frappe's own link check.

### Receipt split holds

A **Receipt Split Hold** (`{request_id}-v{version}`, child table Receipt Split Hold Line) is one
version of an approved receipt's split: per line, an advance or a purchase order line, the order
line it books against, its unit, factor and quantity. Status Held (reserves its quantity), Rejected
(did not fit; holds nothing), Released (given back) or Consumed (posted). Only the API and the
posting write it, under row locks; nobody creates, edits or deletes one at the desk, and changes are
tracked. A Held hold's `live_request_id` is unique, so the database allows one live hold per request.

`fuelbuddy_procurement.api.receipt_split_hold` (erp-functions calls it; see its docstring for the
full contract). Each answers `{ok, code, message, retryable, result, hold, refusals}`:

| Method | | |
|---|---|---|
| `place(request_id, version, supplier, company, item_code, op_key, qty_litres, lines, uom_factors)` | POST | Holds a version (HELD), or stores why not (REJECTED, refusals). Replays, VERSION_CONFLICT, SUPERSEDED, RELEASED (tombstoned) and CONSUMED as the version stands. A new version releases the request's live one (REPLACED) whatever its own outcome. |
| `release(request_id, up_to_version, reason)` | POST | Releases the live version <= up_to_version: RELEASED, ALREADY_RELEASED, NOT_RELEASABLE (posted). Leaves a tombstone for a version never seen. |
| `status(request_id, check, uom_factors)` / `status(live=1)` | GET | Every version and the live one; with check, re-tests the live hold as posting will. Or every live hold. |
| `suggest_split(supplier, company, item_code, qty_litres, for_request, uom_factors)` | GET | The sources net of holds and the oldest-first pre-fill: advances oldest first, each taking all it can, then order lines oldest first. |

`place` checks every line (open, in scope, the unit converting as `uom_factors` says, no more than
available after the earlier lines of the split on the same order line) and the split (total, one
setup). It does not check the order of the lines or how much each advance takes. Refusal codes:
PARC_NOT_OPEN, PARC_SKIPPED, PARC_STUCK, PO_NOT_OPEN, SOURCE_MISMATCH, UOM_FACTOR_MISMATCH, SHORT,
DUPLICATE_SOURCE, TOTAL_MISMATCH, SETUP_MISMATCH. Each write is one transaction that waits at most
8 s for a row lock (`innodb_lock_wait_timeout`); a lock wait or deadlock answers LOCK_RETRY and
stores nothing.

**Posting.** The lane's Purchase Receipt names its hold in `custom_receipt_split_hold` (Purchase
Receipt field, `install.py`) and carries the hold's op key (`custom_app_op_key`, fuelbuddy_crm). On
every save it is refused (`ReceiptHoldRefusedError`, a `ParcRefusedError`) unless the hold is Held and
the rows are its lines (order line, advance, item, unit, quantity within 0.01). On submit, after
ERPNext's own posting and in its transaction, the hold and its lines are re-checked under locks
(leaving the hold itself out of what is held), its advance lines are booked (tagged with the hold),
used-up advances close, and the hold becomes Consumed. A receipt cancelled later gives its quantity
back as any receipt does; its hold stays Consumed. A hold ends only when it is posted, or released
because its request is cancelled or rejected (or its posting is refused); there is no release at the
desk.

### Locks

Every path that decides on quantity takes its locks in one order, so that no two wait on each
other the other way round: purchase order lines (FOR UPDATE, by name), their orders (share), the
advances (FOR UPDATE, oldest first: payment date, then entry, then name), the hold rows it changes
(FOR UPDATE), then the live hold lines (LOCK IN SHARE MODE). A locking read sees the latest
committed rows, so of two contenders the later one sees what the earlier one used or held. A
receipt's own posting in ERPNext locks its order lines first and can still end in a deadlock or lock
wait timeout; nothing is saved then, and it can be sent again.

### Stuck advances

`stamp_stuck_advances` runs daily. It stamps PARC **Stuck Since** (a list filter) on every advance
that is stuck, clears it once it is not, and raises one Issue per stuck advance for the Purchase team
the way the stock lane routes its Issues: Issue `custom_app_issue_key` = `parc-stuck-<advance>`,
`custom_lane_team` = Purchase, Issue Type "Lane Check Mismatch" (fields, type and SLA come from
fuelbuddy_crm's `add_lane_issue_fields`). The Issue is re-opened while the advance stays stuck and
resolved once it is not. Without those fields it writes one Error Log naming the stuck advances.

### Lookups for the approval screen (read-only, GET only)

```
GET /api/method/fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control.get_open_advances?supplier=<Supplier ID>[&company=<Company>][&item_code=<Item>][&for_request=<request id>]
```

The supplier's open advances with quantity left, oldest first: `name`, `purchase_order`,
`purchase_order_item`, `payment_entry`, `payment_date`, `advance_paid`,
`qty_to_be_received_against_the_advance_paid`, `qty_consumed`, `qty_remaining`, `uom_of_item`,
`conversion_factor`, `company`, `po_status`, `skipped`, `stuck`, `qty_held`, `qty_available`,
`qty_available_litres`, `held_by` (`[{hold, request_id, version, qty, qty_litres}]`), `setup_key`,
`unit_problem`. `for_request` leaves that request's own live hold out of what is held. Needs read
permission on PARC.

```
GET /api/method/fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.purchase_advance_receipt_control.get_open_purchase_orders?supplier=<Supplier ID>[&company=<Company>][&item_code=<Item>][&for_request=<request id>]
```

The supplier's submitted purchase order lines with quantity still to receive, oldest order first
(order date, entry order, name, line), leaving out orders that are Closed or On Hold:
`purchase_order`, `transaction_date`, `schedule_date`, `company`, `purchase_order_item`,
`item_code`, `uom`, `conversion_factor`, `qty`, `received_qty`, `qty_to_receive`, `stuck` (always
false), `qty_held`, `qty_available`, `qty_available_litres`, `held_by`, `setup_key`, `unit_problem`.
Needs read permission on Purchase Order.

### Tests

The no-bench modules use a stand-in `frappe` when the real one is not installed; the bench modules
need a site with ERPNext. Bench tests create real documents and hold their locks while they run: use
a lab or staging site, never production.

- `purchase_advance_receipt_control/test_parc_rules.py` (no bench): what an advance has left, each
  desk refusal (net of holds), booking and closing, cancel giving back and re-opening, the
  payment-cancel guard (receipts and holds), the lookups, the hooks wiring, the DocType JSON and the
  install fields.
- `receipt_split_hold/test_receipt_split_hold_rules.py` (no bench): the formula, skipped and stuck
  advances, setup groups, the pre-fill, rounding to a gallon unit, every refusal, typed splits, the
  API's versions on an in-memory hold table, posting a hold, the desk purchase order rules and flag,
  the daily check and the DocTypes.

  ```bash
  python -m unittest fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.test_parc_rules fuelbuddy_procurement.fuelbuddy_procurement.doctype.receipt_split_hold.test_receipt_split_hold_rules
  ```

- `purchase_advance_receipt_control/test_parc_named_advances.py` and
  `receipt_split_hold/test_receipt_split_holds.py` (bench): desk receipts using advances up across
  receipts; holds placed, replayed, superseded, tombstoned, released and posted, desk receipts and
  payment cancels against holds, three advances and two order lines on one receipt, stuck advances.
  Each test makes a supplier of its own and copies the latest submitted Purchase Order; everything
  is rolled back (the holds API's commits run inside a savepoint).

  ```bash
  bench --site <site> execute fuelbuddy_procurement.fuelbuddy_procurement.doctype.purchase_advance_receipt_control.test_parc_named_advances.run
  bench --site <site> execute fuelbuddy_procurement.fuelbuddy_procurement.doctype.receipt_split_hold.test_receipt_split_holds.run
  ```

- `receipt_split_hold/test_receipt_split_hold_races.py` (bench, **commits**): two sessions racing for
  one advance, for one order line, a place against a desk receipt and against a payment cancel, with
  a gate connection holding the contested row; N rounds each. It leaves its documents on the site:
  lab sites only.

  ```bash
  bench --site <site> execute fuelbuddy_procurement.fuelbuddy_procurement.doctype.receipt_split_hold.test_receipt_split_hold_races.run --kwargs "{'rounds': 50}"
  ```

`bench execute` exits 0 even when tests fail: read the JSON on the last line.

### DB script equivalents

`db_scripts/` holds pasteable Server Script bodies for the Payment Entry side and the receipt
cancel (for a site that cannot take the app yet), plus a Purchase Receipt Client Script that shows
the supplier's advances and order lines with what each has available net of holds. See
`db_scripts/README.md` for names, events and the one-mechanism-at-a-time rule.

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/shantanumishra-FB/fuelbuddy_procurement.git --branch main
bench install-app fuelbuddy_procurement
```

`bench migrate` adopts the existing custom DocType **Purchase Advance Receipt Control** into this
app's module (data untouched) and creates **Purchase Advance Consumption**, **Receipt Split Hold**
and **Receipt Split Hold Line**, and the PARC and consumption fields `stuck_since` and
`receipt_split_hold`. The `after_install` hook creates the Purchase Receipt Item and Purchase Receipt
fields, disables the legacy Server Scripts so nothing runs twice (delete them from the DB once the
app is live), and fills `qty_consumed` and `qty_remaining` on open advances. `after_migrate` keeps
the fields and those quantities in step on sites that already have the app.

The desk receipt's hard cap is off until switched on per site:

```bash
bench --site <site> set-config -p fuelbuddy_procurement_desk_rules 1   # 0 switches it off again
```

Open advances from before this change have no consumption rows, so each counts as unused. Review
them before go-live.

Record names (`PARC-26-27-000000001`) come from the site's **Document Naming Rule** (prefix
`PARC.-.FY.-.`, 9 digits). That is data, not part of this app, and keeps working unchanged.

### License

mit
