# PRD: Stock Tracker

| | |
|---|---|
| **Status** | v1 built and tested |
| **Author** | Anurima Majhi |
| **Related** | [DESIGN.md](DESIGN.md) (data model and flows) · [SCREENS.md](SCREENS.md) (screen spec) · [README.md](README.md) |

## 1. Problem

A small warehouse takes orders against stock it may not have yet, or that is already promised to someone else. When stock is tracked in a spreadsheet:

- Two people can promise the same last units to two different orders.
- Damaged or quality-check stock gets promised as if it were sellable.
- A double-click or a retried request records the same receipt or dispatch twice.
- Nobody can say *why* a balance is what it is, because there is no history.

## 2. Goal

One place where the warehouse can see, for every item, how much stock it physically has, how much is blocked, how much is promised, and how much is **free to promise**. The numbers must stay correct when people act at the same time or repeat a request.

**Success looks like:**
- Available stock is never negative and never over-promised.
- Every balance can be rebuilt from the movement history.
- A repeated request never changes stock twice.

## 3. Users

| User | What they do |
|---|---|
| Store keeper | Adds items, receives stock from suppliers, puts stock on hold and takes it off |
| Sales / order desk | Reserves stock against customer orders, cancels when the customer cancels |
| Dispatch | Ships reserved stock, in full or in part |
| Supervisor | Releases reservations that are stuck, checks the movement history |

v1 has no login, so the app can't tell these users apart. See §9.

## 4. Definitions

| Term | Meaning |
|---|---|
| **On hand** | Units physically in the warehouse |
| **Held** | Units on hand that can't be promised (quality check, damage) |
| **Reserved** | Units on hand promised to an open order but not yet dispatched |
| **Available** | `on_hand − held − reserved`: units free to promise |
| **Reservation** | A promise of a quantity of one item to one order |
| **Movement** | One line in the history: one action, on one item, with the balances after it |
| **Request key** | A one-time ID sent with every change, so a repeat of the same request is recognised (idempotency key) |

## 5. Business rules

| ID | Rule |
|---|---|
| BR-01 | `available = on_hand − held − reserved` |
| BR-02 | On hand, held, reserved and available can never be below 0. The database enforces this, not just the app. |
| BR-03 | Only available stock can be reserved or put on hold. |
| BR-04 | A reservation can be dispatched in several parts, but never more than its reserved quantity in total. |
| BR-05 | Release and cancel return only the **undispatched** part of a reservation to available. Dispatched stock stays shipped. |
| BR-06 | Once a reservation is FULFILLED, RELEASED or CANCELLED, it can't be changed. |
| BR-07 | Every change to a balance writes a movement in the same transaction. Either both are saved or neither is. |
| BR-08 | A request repeated with the same request key returns the first result and changes nothing. |
| BR-09 | A request key used for one action can't be reused for a different action. |
| BR-10 | Only one change is applied at a time, so two people can't both take the same last units. |
| BR-11 | SKUs are unique, trimmed and stored in upper case. |

## 6. Requirements

| ID | Requirement | Rules | Priority |
|---|---|---|---|
| REQ-01 | Add an item with a SKU and name. It starts at zero stock. | BR-11 | Must |
| REQ-02 | Receive a positive quantity of an item into on hand, with an optional note (e.g. GRN number). | BR-07 | Must |
| REQ-03 | Put a quantity of available stock on hold, with an optional reason. | BR-03 | Must |
| REQ-04 | Take a quantity off hold. | BR-02 | Must |
| REQ-05 | Reserve a quantity of available stock against an order reference. This returns a reservation ID. | BR-03, BR-10 | Must |
| REQ-06 | Dispatch part or all of what is left on an open reservation. | BR-04 | Must |
| REQ-07 | Release an open reservation, for when the stock is re-planned. | BR-05, BR-06 | Must |
| REQ-08 | Cancel an open reservation, for when the customer cancels. | BR-05, BR-06 | Must |
| REQ-09 | Show every item with on hand, held, reserved and available. | BR-01 | Must |
| REQ-10 | Show every reservation with its quantity, dispatched, remaining and status. | | Must |
| REQ-11 | Show the latest movements with the balances after each one. | BR-07 | Must |
| REQ-12 | Check on every page load that balances match a rebuild from the movement history, and show the result. | BR-07 | Should |
| REQ-13 | Ignore a repeated submit of the same form and say so. | BR-08 | Must |
| REQ-14 | Show a clear message when an action is refused, with the reason and the numbers. | | Must |

## 7. Acceptance criteria

**REQ-02 Receive**
- *Given* RICE has on hand 0, *when* I receive 100, *then* on hand is 100, available is 100, and a RECEIVE movement shows 100 / 0 / 0.
- *Given* any item, *when* I receive 0, −5 or "abc", *then* it is refused with "Quantity must be greater than 0." or "Quantity must be a whole number.", and nothing changes.

**REQ-03 Hold**
- *Given* on hand 10 and reserved 8, *when* I hold 3, *then* it is refused: only 2 are available to hold.

**REQ-05 Reserve**
- *Given* on hand 10 and held 4, *when* I reserve 7, *then* it is refused with "Cannot reserve 7 of RICE: only 6 available."
- *Given* on hand 10 and held 4, *when* I reserve 6 for SO-1, *then* a reservation is created with status ACTIVE, reserved is 6, and available is 0.
- *Given* 10 available, *when* 20 people each reserve 1 at the same moment, *then* exactly 10 succeed, 10 are refused, and available is 0.
- *Given* any item, *when* I reserve without an order reference, *then* it is refused.

**REQ-06 Dispatch**
- *Given* reservation R for 20, *when* I dispatch 12, *then* R is PARTIALLY_DISPATCHED with 8 remaining, on hand falls by 12, and reserved falls by 12.
- *Given* reservation R with 5 remaining, *when* I dispatch 6, *then* it is refused with "Only 5 left to dispatch on R."
- *Given* reservation R with 5 remaining, *when* I dispatch 5, *then* R is FULFILLED.

**REQ-07 / REQ-08 Release and cancel**
- *Given* R for 20 with 12 dispatched, *when* I release R, *then* reserved falls by 8 (not 20), on hand is unchanged, and R is RELEASED.
- *Given* R is already CANCELLED, *when* I cancel it again, *then* it is refused with "Cannot cancel R: it is already CANCELLED."
- *Given* R is FULFILLED, *when* I cancel it, *then* it is refused.

**REQ-13 Duplicate requests**
- *Given* I receive 10 with request key K, *when* the same request with K arrives again, *then* on hand rises by 10 only once, and the second response is marked as a duplicate.
- *Given* I reserve with key K, *when* the same request with K arrives again, *then* the same reservation ID is returned and only one reservation exists.
- *Given* key K was used to receive, *when* K is sent with a hold, *then* it is refused.

**REQ-12 Ledger check**
- *Given* any sequence of the actions above, *when* the page loads, *then* it shows "Ledger matches balances."

Each criterion above has an automated test in `test_inventory.py`.

## 8. Out of scope for v1

- Logins, roles and permissions
- More than one warehouse, and bin or location tracking
- Purchase orders, supplier records, invoices
- Automatic expiry of old reservations
- Batch or lot numbers and expiry dates
- Connecting to an ERP (Tally, Zoho, SAP)

## 9. Open questions

| # | Question | Who can answer |
|---|---|---|
| Q1 | Should only a supervisor be able to release a reservation, or anyone? | Warehouse manager |
| Q2 | How long should an unused reservation stay open before it is released automatically? 24 hours? 7 days? | Sales head |
| Q3 | When received stock turns out damaged, does it go on hold, or get written off (removed from on hand)? v1 has no write-off. | Warehouse manager, finance |
| Q4 | Can a dispatch be reversed, for example when the customer returns goods? v1 has no returns. | Operations head |
| Q5 | Does the ledger need to record *who* did each action, for audit? | Finance |
| Q6 | Which system is the source of truth if this connects to an ERP later: this app or the ERP? | IT, finance |
