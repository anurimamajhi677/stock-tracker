# Design: Stock Tracker

How the data is stored, how a reservation moves through its life, and what happens step by step when things go wrong. The rules referenced here (BR-xx) are in [PRD.md](PRD.md).

GitHub draws the diagrams below automatically (they are written in Mermaid).

## 1. Data model

```mermaid
erDiagram
    ITEM ||--|| BALANCE : "has one"
    ITEM ||--o{ RESERVATION : "is reserved by"
    ITEM ||--o{ MOVEMENT : "is changed by"
    RESERVATION ||--o{ MOVEMENT : "causes"

    ITEM {
        text sku PK "unique, upper case"
        text name
    }
    BALANCE {
        text sku PK "FK to ITEM"
        int on_hand "at least 0"
        int held "at least 0"
        int reserved "at least 0"
    }
    RESERVATION {
        text id PK "e.g. R-1E786D47"
        text sku FK
        text order_ref "e.g. SO-1042"
        int qty "more than 0"
        int qty_dispatched "0 to qty"
        text status "ACTIVE, PARTIALLY_DISPATCHED, FULFILLED, RELEASED, CANCELLED"
        text created_at
    }
    MOVEMENT {
        int id PK "auto, append-only"
        text at
        text sku
        text action "RECEIVE, HOLD, UNHOLD, RESERVE, DISPATCH, RELEASE, CANCEL"
        int qty
        text reservation_id "if any"
        text note
        int on_hand_after
        int held_after
        int reserved_after
    }
    IDEMPOTENCY {
        text key PK "one per request"
        text action
        text result "first response, as JSON"
        text created_at
    }
```

### Source of truth

| Number | Source of truth | Check |
|---|---|---|
| On hand, held, reserved | `BALANCE` row | Rebuilt from `MOVEMENT` on every page load (REQ-12) |
| Available | Calculated, never stored: `on_hand − held − reserved` | Database constraint `on_hand − held − reserved ≥ 0` (BR-02) |
| Reservation progress | `RESERVATION.qty_dispatched` and `status` | Constraint `qty_dispatched ≤ qty` |
| Has this request run already? | `IDEMPOTENCY` row | Primary key on `key` |

**Why store balances and keep a ledger?** Rebuilding balances from the ledger on every read is slow once history grows. Storing only balances loses the *why*. Doing both, in one transaction (BR-07), gives fast reads and a full history, and the ledger check catches it if they ever drift apart.

**Why is available never stored?** A stored copy could drift from the three numbers it depends on. Calculating it means it is always right.

## 2. Reservation status flow

```mermaid
stateDiagram-v2
    [*] --> ACTIVE : reserve
    ACTIVE --> PARTIALLY_DISPATCHED : dispatch part
    ACTIVE --> FULFILLED : dispatch all
    PARTIALLY_DISPATCHED --> PARTIALLY_DISPATCHED : dispatch more, some left
    PARTIALLY_DISPATCHED --> FULFILLED : dispatch the rest
    ACTIVE --> RELEASED : release
    ACTIVE --> CANCELLED : cancel
    PARTIALLY_DISPATCHED --> RELEASED : release the rest
    PARTIALLY_DISPATCHED --> CANCELLED : cancel the rest
    FULFILLED --> [*]
    RELEASED --> [*]
    CANCELLED --> [*]
```

Closed statuses (FULFILLED, RELEASED, CANCELLED) accept no further actions (BR-06).

### What each action does to the balance

| Action | on_hand | held | reserved | available |
|---|:-:|:-:|:-:|:-:|
| Receive q | +q | | | +q |
| Hold q | | +q | | −q |
| Unhold q | | −q | | +q |
| Reserve q | | | +q | −q |
| Dispatch q | −q | | −q | 0 |
| Release / cancel (r left) | | | −r | +r |

Dispatch doesn't change available: the units were already promised, and now they have left.

### Worked example

| Step | on_hand | held | reserved | available |
|---|:-:|:-:|:-:|:-:|
| Receive 100 | 100 | 0 | 0 | 100 |
| Hold 10 (damaged) | 100 | 10 | 0 | 90 |
| Reserve 30 for SO-1042 | 100 | 10 | 30 | 60 |
| Dispatch 20 on SO-1042 | 80 | 10 | 10 | 60 |
| Cancel SO-1042 (10 left) | 80 | 10 | 0 | 70 |

## 3. Sequence flows

### 3.1 Reserve (normal path)

```mermaid
sequenceDiagram
    actor U as Order desk
    participant P as Web page
    participant A as App
    participant D as SQLite

    U->>P: Fill SKU, qty, order ref. Click Reserve
    P->>A: POST /reserve (sku, qty, order_ref, key K)
    A->>D: BEGIN IMMEDIATE (take write lock)
    A->>D: Has key K run before?
    D-->>A: No
    A->>D: Read balance. Is qty ≤ available?
    D-->>A: Yes
    A->>D: Insert reservation (ACTIVE)
    A->>D: reserved += qty, insert RESERVE movement
    A->>D: Save key K with result
    A->>D: COMMIT (release lock)
    A-->>P: Redirect with "Reserved 30 of RICE for SO-1042 (R-…)"
    P-->>U: Updated page and message
```

### 3.2 Two people reserve the last units at the same time

```mermaid
sequenceDiagram
    actor U1 as Person 1
    actor U2 as Person 2
    participant A as App
    participant D as SQLite

    Note over D: available = 1
    U1->>A: Reserve 1 (key K1)
    U2->>A: Reserve 1 (key K2)
    A->>D: [K1] BEGIN IMMEDIATE, gets lock
    A->>D: [K2] BEGIN IMMEDIATE, waits for lock
    A->>D: [K1] available is 1, reserve, COMMIT
    Note over D: available = 0
    D-->>A: [K2] gets lock
    A->>D: [K2] available is 0, refuse, ROLLBACK
    A-->>U1: Reserved
    A-->>U2: "Cannot reserve 1 of RICE: only 0 available."
```

Without the lock, both would read "available = 1" before either saved, and both would succeed. That's the over-promise the app exists to prevent (BR-10).

### 3.3 Double-click or network retry

```mermaid
sequenceDiagram
    actor U as Store keeper
    participant A as App
    participant D as SQLite

    U->>A: Receive 50 (key K)
    A->>D: K not seen. on_hand += 50. Save K with result. COMMIT
    A--xU: Response lost (network drop)
    U->>A: Retry: Receive 50 (same key K)
    A->>D: K seen before
    D-->>A: Saved result
    A-->>U: "Received 50 of RICE. (duplicate submit ignored)"
    Note over D: on_hand rose by 50 once, not 100
```

Each form gets a new key when the page loads, so the same form sent twice carries the same key. A new action after the page reloads gets a new key.

### 3.4 Failure partway through

If anything fails after `BEGIN IMMEDIATE` (a rule check, a database constraint, a crash), the whole transaction is rolled back. The balance, movement, reservation and request key are saved together or not at all (BR-07). A failed request doesn't save its key, so retrying it re-checks against current stock.

## 4. Technology choices

| Choice | Why | Trade-off |
|---|---|---|
| Python standard library | Runs on any Mac or Linux machine with no installs | No framework conveniences (forms, templates) |
| SQLite | Real transactions and constraints in a single file | One writer at a time. Fine for one site, but many sites would need Postgres with row-level locks |
| Server-rendered HTML | One page, no JavaScript build step | Full page reload on every action |
| Post/Redirect/Get | A browser refresh doesn't resubmit the form | One extra request per action |
