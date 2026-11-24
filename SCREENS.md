# Screen spec: Stock Tracker

v1 has **one screen**, the Stock Tracker page at `/`. Every action is a form on this page. After each action, the page reloads and shows a message at the top. Requirement IDs (REQ-xx) and rules (BR-xx) are in [PRD.md](PRD.md).

## 1. Entry points

| From | How |
|---|---|
| Browser | Open `http://localhost:8000` |
| After any action | The app redirects back to `/` with a success or error message (Post/Redirect/Get) |

## 2. Layout (top to bottom)

| # | Section | Purpose | Req |
|---|---|---|---|
| 1 | Header | Title, the formula `available = on-hand − held − reserved`, and the ledger check result | REQ-12 |
| 2 | Message banner | Result of the last action: green for success, red for refused | REQ-14 |
| 3 | Stock table | One row per item | REQ-09 |
| 4 | Action cards | Add item, receive stock, reserve for an order, quality hold. Side by side on wide screens, stacked on phones | REQ-01 to 05 |
| 5 | Reservations table | One row per reservation, newest first, with action buttons on open ones | REQ-06 to 08, REQ-10 |
| 6 | Movement ledger | Latest 30 movements, newest first | REQ-11 |

## 3. Data shown

**Stock table:** SKU · Name · On hand · Held · Reserved · **Available** (bold). Numbers are right-aligned.

**Reservations table:** ID · Order · SKU · Qty · Dispatched · Remaining · Status · Actions

**Movement ledger:** When (UTC) · SKU · Action · Qty · Reservation · Note · On hand / Held / Reserved after

## 4. Actions and validations

Every form carries a hidden one-time request key (BR-08). Fields marked * are required.

| Action | Fields | Checked in the browser | Checked by the app | Refused with |
|---|---|---|---|---|
| **Add item** | SKU*, Name* | Both filled | Both non-blank after trimming; SKU not already used | "SKU and name are both required." / "SKU 'X' already exists." |
| **Receive** | SKU* (dropdown), Qty*, Note | Qty ≥ 1 | Qty is a whole number > 0; SKU exists | "Quantity must be a whole number." / "Quantity must be greater than 0." / "Unknown SKU 'X'." |
| **Reserve** | SKU*, Qty*, Order ref* | Qty ≥ 1, order ref filled | Qty ≤ available; order ref not blank | "Cannot reserve 7 of RICE: only 6 available." / "Order reference is required." |
| **Hold** | SKU*, Qty*, Reason | Qty ≥ 1 | Qty ≤ available | "Only 2 of RICE available to hold." |
| **Unhold** | SKU*, Qty* | Qty ≥ 1 | Qty ≤ held | "Only 3 of RICE is on hold." |
| **Dispatch** (row button) | Qty* (pre-filled with remaining) | 1 ≤ qty ≤ remaining | Qty ≤ remaining; reservation still open | "Only 5 left to dispatch on R-…" / "Cannot dispatch R-…: it is already FULFILLED." |
| **Release** (row button) | none | | Reservation still open | "Cannot release R-…: it is already RELEASED." |
| **Cancel** (row button) | none | | Reservation still open | "Cannot cancel R-…: it is already CANCELLED." |

Browser checks are only for convenience. The app checks everything again, because a request can skip the browser.

**Row buttons:** Dispatch, Release and Cancel appear only on reservations that are ACTIVE or PARTIALLY_DISPATCHED. Closed reservations show no buttons (BR-06).

## 5. States

| State | What the user sees |
|---|---|
| **Empty: no items** | Stock table shows "No items yet. Add one below." SKU dropdowns show "Add an item first". |
| **Empty: no reservations** | "No reservations yet." |
| **Empty: no movements** | "Nothing has happened yet." |
| **Success** | Green banner, e.g. "Reserved 30 of RICE-25KG for SO-1042 (R-1E786D47)." |
| **Duplicate submit** | Green banner with "(duplicate submit ignored)" added. Nothing changes twice. |
| **Refused** | Red banner with the reason and the numbers. Nothing changes. |
| **Ledger mismatch** | The header shows "LEDGER MISMATCH" with the SKU and both sets of numbers. This should never happen, and it's there to catch bugs. |
| **Loading** | None. The page reloads after each action, and the browser's own loading indicator shows. |
| **Restricted** | None in v1, since there are no logins and everyone can do every action. See PRD open question Q1. |
| **Server down** | The browser shows its own "can't connect" page. A form submitted during an outage is not applied, and resubmitting it later is safe (BR-08). |

## 6. Known gaps for v2

- No search or filter. Fine for tens of items, but not hundreds.
- The ledger shows only the latest 30 movements, with no paging.
- No confirmation step before Cancel or Release. A mis-click can't be undone in v1, and the reservation has to be made again.
- No way to edit an item's name.
