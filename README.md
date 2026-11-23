# Stock Tracker

A small warehouse stock and reservation app. You can receive stock, reserve it for orders, dispatch it (in full or in part), release or cancel reservations, and put stock on quality hold.

**Stack:** Python 3.9+ and SQLite, standard library only (nothing to install).
**Why:** It is the smallest stack that still gives real transactions and database constraints, so the rules below are enforced and not just described.

## Run it

```bash
python3 app.py          # open http://localhost:8000
python3 -m unittest -v  # 13 tests
```

Delete `stock.db` to start fresh.

## The rule

```
available = on_hand − held − reserved      (never below 0)
```

| Action   | on_hand | held | reserved | Allowed when |
|----------|:------:|:----:|:--------:|--------------|
| Receive  | +q | | | q > 0 |
| Hold     | | +q | | q ≤ available |
| Unhold   | | −q | | q ≤ held |
| Reserve  | | | +q | q ≤ available |
| Dispatch | −q | | −q | q ≤ what is left on that reservation |
| Release  | | | −left | reservation is still open |
| Cancel   | | | −left | reservation is still open |

**Release vs cancel:** both return the undispatched quantity to available. The difference is the reason. Release means the stock is being re-planned (e.g. the reservation expired). Cancel means the customer cancelled. Anything already dispatched stays shipped.

**Reservation statuses:** `ACTIVE → PARTIALLY_DISPATCHED → FULFILLED`. From either of the first two, it can move to `RELEASED` or `CANCELLED`. Closed reservations cannot be changed.

## Source of truth

- `balance` holds the current numbers for each SKU.
- `movement` is an append-only ledger. Every balance change writes a ledger row in the **same transaction**, so the two can't disagree. The page header rebuilds balances from the ledger on every load and shows "Ledger matches balances" (or a mismatch).

## What happens when things go wrong

| Situation | What the system does |
|-----------|----------------------|
| Two people reserve the last unit at the same time | Each write takes a write lock (`BEGIN IMMEDIATE`), so the second one sees the updated balance and is refused. Test: 20 threads race for 10 units, and exactly 10 win. |
| Double-click, or a retried request after a network drop | Each form carries a one-time key (idempotency key). A repeat with the same key returns the first result and changes nothing. |
| Same key used for a different action | Refused. |
| Bug in the code tries to make stock negative | The database rejects it (`CHECK` constraints), and the whole transaction rolls back. |
| Dispatch more than reserved, or cancel twice | Refused with a clear message. |
| Browser refresh after submitting | Post/Redirect/Get, so a refresh doesn't resubmit. |

## Known limits (what I'd add next)

- No users or roles. Anyone can do anything, and the ledger doesn't record who did it.
- One warehouse, no bin or location tracking.
- Reservations don't expire on their own. Release is manual.
- A failed request is not stored against its key, so retrying it re-checks against current stock. That's deliberate: if stock arrives later, the retry can succeed.
- SQLite is fine for one site. Many sites writing at once would need Postgres with row-level locks.
