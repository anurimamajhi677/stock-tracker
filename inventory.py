"""Stock & reservation logic on SQLite.

Rule:  available = on_hand - held - reserved   (never below 0)

Source of truth:
  - `balance` holds the current on_hand / held / reserved per SKU.
  - `movement` is an append-only ledger. Every change to `balance` writes
    a movement row in the SAME transaction, so the ledger always explains
    the balance.
  - `reservation` tracks each reservation and how much of it is dispatched.

Safety:
  - Every write runs inside BEGIN IMMEDIATE, so only one writer at a time.
    Two people reserving the last unit cannot both succeed.
  - CHECK constraints in the schema make negative stock impossible even if
    the Python code has a bug.
  - Every write takes an idempotency key. Repeating a request with the same
    key returns the first result instead of applying it twice
    (double-click, network retry).
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS item (
    sku   TEXT PRIMARY KEY,
    name  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS balance (
    sku       TEXT PRIMARY KEY REFERENCES item(sku),
    on_hand   INTEGER NOT NULL DEFAULT 0 CHECK (on_hand  >= 0),
    held      INTEGER NOT NULL DEFAULT 0 CHECK (held     >= 0),
    reserved  INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    CHECK (on_hand - held - reserved >= 0)
);

CREATE TABLE IF NOT EXISTS reservation (
    id              TEXT PRIMARY KEY,
    sku             TEXT NOT NULL REFERENCES item(sku),
    order_ref       TEXT NOT NULL,
    qty             INTEGER NOT NULL CHECK (qty > 0),
    qty_dispatched  INTEGER NOT NULL DEFAULT 0 CHECK (qty_dispatched >= 0),
    status          TEXT NOT NULL CHECK (status IN
                      ('ACTIVE','PARTIALLY_DISPATCHED','FULFILLED','RELEASED','CANCELLED')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (qty_dispatched <= qty)
);

CREATE TABLE IF NOT EXISTS movement (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT NOT NULL DEFAULT (datetime('now')),
    sku             TEXT NOT NULL,
    action          TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    reservation_id  TEXT,
    note            TEXT,
    on_hand_after   INTEGER NOT NULL,
    held_after      INTEGER NOT NULL,
    reserved_after  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    key         TEXT PRIMARY KEY,
    action      TEXT NOT NULL,
    result      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

OPEN_STATUSES = ("ACTIVE", "PARTIALLY_DISPATCHED")


class StockError(Exception):
    """A business rule was broken. The message is safe to show to users."""


class Inventory:
    def __init__(self, path="stock.db"):
        self.path = path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    # ---------- plumbing ----------

    def _connect(self):
        # isolation_level=None: we control transactions ourselves.
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _write(self, idem_key, action):
        """One write = one IMMEDIATE transaction, with idempotency.

        Yields (conn, previous_result). If previous_result is not None the
        request was already applied, and the caller must just return it.
        """
        if not idem_key:
            raise StockError("Missing idempotency key.")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT action, result FROM idempotency WHERE key = ?", (idem_key,)
            ).fetchone()
            if row is not None:
                if row["action"] != action:
                    raise StockError("This request key was already used for a different action.")
                yield conn, json.loads(row["result"])
            else:
                yield conn, None
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _save_result(conn, idem_key, action, result):
        conn.execute(
            "INSERT INTO idempotency (key, action, result) VALUES (?, ?, ?)",
            (idem_key, action, json.dumps(result)),
        )
        result = dict(result)
        result["replayed"] = False
        return result

    @staticmethod
    def _balance(conn, sku):
        row = conn.execute("SELECT * FROM balance WHERE sku = ?", (sku,)).fetchone()
        if row is None:
            raise StockError("Unknown SKU '%s'." % sku)
        return row

    @staticmethod
    def _apply(conn, sku, action, qty, d_on_hand=0, d_held=0, d_reserved=0,
               reservation_id=None, note=None):
        """Change the balance and write the ledger row, together."""
        conn.execute(
            "UPDATE balance SET on_hand = on_hand + ?, held = held + ?, "
            "reserved = reserved + ? WHERE sku = ?",
            (d_on_hand, d_held, d_reserved, sku),
        )
        b = conn.execute("SELECT * FROM balance WHERE sku = ?", (sku,)).fetchone()
        conn.execute(
            "INSERT INTO movement (sku, action, qty, reservation_id, note, "
            "on_hand_after, held_after, reserved_after) VALUES (?,?,?,?,?,?,?,?)",
            (sku, action, qty, reservation_id, note, b["on_hand"], b["held"], b["reserved"]),
        )

    @staticmethod
    def _positive_int(qty):
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            raise StockError("Quantity must be a whole number.")
        if qty <= 0:
            raise StockError("Quantity must be greater than 0.")
        return qty

    @staticmethod
    def _available(b):
        return b["on_hand"] - b["held"] - b["reserved"]

    # ---------- reads ----------

    def stock(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT i.sku, i.name, b.on_hand, b.held, b.reserved, "
                "b.on_hand - b.held - b.reserved AS available "
                "FROM item i JOIN balance b USING (sku) ORDER BY i.sku"
            ).fetchall()
        return [dict(r) for r in rows]

    def reservations(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT *, qty - qty_dispatched AS remaining FROM reservation "
                "ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def movements(self, limit=50):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM movement ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def check_ledger(self):
        """Rebuild every balance from the ledger and compare. Returns mismatches."""
        deltas = {
            "RECEIVE": (1, 0, 0), "HOLD": (0, 1, 0), "UNHOLD": (0, -1, 0),
            "RESERVE": (0, 0, 1), "RELEASE": (0, 0, -1), "CANCEL": (0, 0, -1),
            "DISPATCH": (-1, 0, -1),
        }
        rebuilt = {}
        with self._connect() as conn:
            for m in conn.execute("SELECT sku, action, qty FROM movement ORDER BY id"):
                o, h, r = rebuilt.get(m["sku"], (0, 0, 0))
                do, dh, dr = deltas[m["action"]]
                rebuilt[m["sku"]] = (o + do * m["qty"], h + dh * m["qty"], r + dr * m["qty"])
            problems = []
            for b in conn.execute("SELECT * FROM balance"):
                expected = rebuilt.get(b["sku"], (0, 0, 0))
                actual = (b["on_hand"], b["held"], b["reserved"])
                if expected != actual:
                    problems.append({"sku": b["sku"], "ledger": expected, "balance": actual})
        return problems

    # ---------- writes ----------

    def add_item(self, sku, name, idem_key):
        sku = (sku or "").strip().upper()
        name = (name or "").strip()
        if not sku or not name:
            raise StockError("SKU and name are both required.")
        with self._write(idem_key, "ADD_ITEM") as (conn, prev):
            if prev is not None:
                return dict(prev, replayed=True)
            if conn.execute("SELECT 1 FROM item WHERE sku = ?", (sku,)).fetchone():
                raise StockError("SKU '%s' already exists." % sku)
            conn.execute("INSERT INTO item (sku, name) VALUES (?, ?)", (sku, name))
            conn.execute("INSERT INTO balance (sku) VALUES (?)", (sku,))
            return self._save_result(conn, idem_key, "ADD_ITEM",
                                     {"message": "Added %s (%s)." % (sku, name)})

    def receive(self, sku, qty, idem_key, note=None):
        qty = self._positive_int(qty)
        with self._write(idem_key, "RECEIVE") as (conn, prev):
            if prev is not None:
                return dict(prev, replayed=True)
            self._balance(conn, sku)
            self._apply(conn, sku, "RECEIVE", qty, d_on_hand=qty, note=note)
            return self._save_result(conn, idem_key, "RECEIVE",
                                     {"message": "Received %d of %s." % (qty, sku)})

    def hold(self, sku, qty, idem_key, note=None):
        """Put available stock on hold (e.g. quality check, damage)."""
        qty = self._positive_int(qty)
        with self._write(idem_key, "HOLD") as (conn, prev):
            if prev is not None:
                return dict(prev, replayed=True)
            b = self._balance(conn, sku)
            if qty > self._available(b):
                raise StockError("Only %d of %s available to hold." % (self._available(b), sku))
            self._apply(conn, sku, "HOLD", qty, d_held=qty, note=note)
            return self._save_result(conn, idem_key, "HOLD",
                                     {"message": "Put %d of %s on hold." % (qty, sku)})

    def unhold(self, sku, qty, idem_key, note=None):
        qty = self._positive_int(qty)
        with self._write(idem_key, "UNHOLD") as (conn, prev):
            if prev is not None:
                return dict(prev, replayed=True)
            b = self._balance(conn, sku)
            if qty > b["held"]:
                raise StockError("Only %d of %s is on hold." % (b["held"], sku))
            self._apply(conn, sku, "UNHOLD", qty, d_held=-qty, note=note)
            return self._save_result(conn, idem_key, "UNHOLD",
                                     {"message": "Took %d of %s off hold." % (qty, sku)})

    def reserve(self, sku, qty, order_ref, idem_key):
        qty = self._positive_int(qty)
        order_ref = (order_ref or "").strip()
        if not order_ref:
            raise StockError("Order reference is required.")
        with self._write(idem_key, "RESERVE") as (conn, prev):
            if prev is not None:
                return dict(prev, replayed=True)
            b = self._balance(conn, sku)
            if qty > self._available(b):
                raise StockError("Cannot reserve %d of %s: only %d available."
                                 % (qty, sku, self._available(b)))
            rid = "R-" + uuid.uuid4().hex[:8].upper()
            conn.execute(
                "INSERT INTO reservation (id, sku, order_ref, qty, status) "
                "VALUES (?, ?, ?, ?, 'ACTIVE')",
                (rid, sku, order_ref, qty),
            )
            self._apply(conn, sku, "RESERVE", qty, d_reserved=qty,
                        reservation_id=rid, note=order_ref)
            return self._save_result(conn, idem_key, "RESERVE", {
                "message": "Reserved %d of %s for %s (%s)." % (qty, sku, order_ref, rid),
                "reservation_id": rid,
            })

    def dispatch(self, reservation_id, qty, idem_key):
        """Ship some or all of what is still reserved. Partial dispatch allowed."""
        qty = self._positive_int(qty)
        with self._write(idem_key, "DISPATCH") as (conn, prev):
            if prev is not None:
                return dict(prev, replayed=True)
            r = self._open_reservation(conn, reservation_id, "dispatch")
            remaining = r["qty"] - r["qty_dispatched"]
            if qty > remaining:
                raise StockError("Only %d left to dispatch on %s." % (remaining, reservation_id))
            new_dispatched = r["qty_dispatched"] + qty
            status = "FULFILLED" if new_dispatched == r["qty"] else "PARTIALLY_DISPATCHED"
            conn.execute(
                "UPDATE reservation SET qty_dispatched = ?, status = ? WHERE id = ?",
                (new_dispatched, status, reservation_id),
            )
            self._apply(conn, r["sku"], "DISPATCH", qty, d_on_hand=-qty, d_reserved=-qty,
                        reservation_id=reservation_id, note=r["order_ref"])
            return self._save_result(conn, idem_key, "DISPATCH", {
                "message": "Dispatched %d of %s on %s. Status: %s."
                           % (qty, r["sku"], reservation_id, status),
            })

    def release(self, reservation_id, idem_key):
        """Give back what is left on a reservation (e.g. it expired, stock re-planned)."""
        return self._close(reservation_id, idem_key, "RELEASE", "RELEASED")

    def cancel(self, reservation_id, idem_key):
        """The order was cancelled. Same stock effect as release, different reason."""
        return self._close(reservation_id, idem_key, "CANCEL", "CANCELLED")

    def _close(self, reservation_id, idem_key, action, status):
        with self._write(idem_key, action) as (conn, prev):
            if prev is not None:
                return dict(prev, replayed=True)
            r = self._open_reservation(conn, reservation_id, action.lower())
            remaining = r["qty"] - r["qty_dispatched"]
            conn.execute("UPDATE reservation SET status = ? WHERE id = ?",
                         (status, reservation_id))
            self._apply(conn, r["sku"], action, remaining, d_reserved=-remaining,
                        reservation_id=reservation_id, note=r["order_ref"])
            return self._save_result(conn, idem_key, action, {
                "message": "%s %s: %d of %s back to available (%d already dispatched stays shipped)."
                           % (status.capitalize(), reservation_id, remaining, r["sku"],
                              r["qty_dispatched"]),
            })

    @staticmethod
    def _open_reservation(conn, reservation_id, verb):
        r = conn.execute("SELECT * FROM reservation WHERE id = ?", (reservation_id,)).fetchone()
        if r is None:
            raise StockError("Reservation %s not found." % reservation_id)
        if r["status"] not in OPEN_STATUSES:
            raise StockError("Cannot %s %s: it is already %s."
                             % (verb, reservation_id, r["status"]))
        return r
