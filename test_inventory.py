"""Run:  python3 -m unittest -v"""

import os
import sqlite3
import tempfile
import threading
import unittest
import uuid

from inventory import Inventory, StockError


def k():
    return uuid.uuid4().hex


class StockTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.inv = Inventory(os.path.join(self.dir.name, "t.db"))
        self.inv.add_item("RICE", "Rice 25kg", k())

    def tearDown(self):
        self.dir.cleanup()

    def bal(self):
        return self.inv.stock()[0]

    # --- the rule ---

    def test_available_formula_traced_through_actions(self):
        self.inv.receive("RICE", 100, k())                      # 100 / 0 / 0
        self.inv.hold("RICE", 10, k())                          # 100 / 10 / 0
        rid = self.inv.reserve("RICE", 30, "SO-1", k())["reservation_id"]  # 100/10/30
        self.inv.dispatch(rid, 20, k())                         # 80 / 10 / 10
        b = self.bal()
        self.assertEqual((b["on_hand"], b["held"], b["reserved"], b["available"]), (80, 10, 10, 60))
        self.inv.cancel(rid, k())                               # 80 / 10 / 0
        self.assertEqual(self.bal()["available"], 70)
        self.assertEqual(self.inv.check_ledger(), [])

    def test_cannot_reserve_more_than_available(self):
        self.inv.receive("RICE", 10, k())
        self.inv.hold("RICE", 4, k())
        with self.assertRaises(StockError):
            self.inv.reserve("RICE", 7, "SO-1", k())            # only 6 available
        self.inv.reserve("RICE", 6, "SO-1", k())
        self.assertEqual(self.bal()["available"], 0)

    def test_cannot_hold_reserved_stock(self):
        self.inv.receive("RICE", 10, k())
        self.inv.reserve("RICE", 8, "SO-1", k())
        with self.assertRaises(StockError):
            self.inv.hold("RICE", 3, k())

    def test_bad_quantities_rejected(self):
        for bad in (0, -5, "abc", None):
            with self.assertRaises(StockError):
                self.inv.receive("RICE", bad, k())

    def test_database_blocks_negative_stock_even_if_code_is_bypassed(self):
        conn = sqlite3.connect(self.inv.path)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE balance SET reserved = 5 WHERE sku = 'RICE'")  # on_hand is 0
        conn.close()

    # --- partial fulfilment ---

    def test_partial_dispatch_then_release_returns_only_the_rest(self):
        self.inv.receive("RICE", 50, k())
        rid = self.inv.reserve("RICE", 20, "SO-1", k())["reservation_id"]
        self.inv.dispatch(rid, 12, k())
        r = self.inv.reservations()[0]
        self.assertEqual((r["status"], r["remaining"]), ("PARTIALLY_DISPATCHED", 8))
        self.inv.release(rid, k())
        b = self.bal()
        self.assertEqual((b["on_hand"], b["reserved"], b["available"]), (38, 0, 38))

    def test_cannot_over_dispatch(self):
        self.inv.receive("RICE", 50, k())
        rid = self.inv.reserve("RICE", 5, "SO-1", k())["reservation_id"]
        with self.assertRaises(StockError):
            self.inv.dispatch(rid, 6, k())

    def test_full_dispatch_marks_fulfilled_and_closes(self):
        self.inv.receive("RICE", 5, k())
        rid = self.inv.reserve("RICE", 5, "SO-1", k())["reservation_id"]
        self.inv.dispatch(rid, 5, k())
        self.assertEqual(self.inv.reservations()[0]["status"], "FULFILLED")
        with self.assertRaises(StockError):
            self.inv.cancel(rid, k())                           # nothing left to cancel

    # --- cancellations ---

    def test_cancel_twice_with_different_keys_is_refused(self):
        self.inv.receive("RICE", 5, k())
        rid = self.inv.reserve("RICE", 5, "SO-1", k())["reservation_id"]
        self.inv.cancel(rid, k())
        with self.assertRaises(StockError):
            self.inv.cancel(rid, k())
        self.assertEqual(self.bal()["reserved"], 0)

    # --- duplicate requests ---

    def test_same_key_applies_once(self):
        key = k()
        first = self.inv.receive("RICE", 10, key)
        second = self.inv.receive("RICE", 10, key)              # double-click / retry
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(self.bal()["on_hand"], 10)

    def test_duplicate_reserve_returns_same_reservation(self):
        self.inv.receive("RICE", 10, k())
        key = k()
        a = self.inv.reserve("RICE", 4, "SO-1", key)
        b = self.inv.reserve("RICE", 4, "SO-1", key)
        self.assertEqual(a["reservation_id"], b["reservation_id"])
        self.assertEqual(len(self.inv.reservations()), 1)

    def test_key_reused_for_different_action_is_refused(self):
        key = k()
        self.inv.receive("RICE", 10, key)
        with self.assertRaises(StockError):
            self.inv.hold("RICE", 1, key)

    # --- concurrency ---

    def test_two_people_race_for_the_last_units(self):
        self.inv.receive("RICE", 10, k())
        wins, losses = [], []
        start = threading.Barrier(20)

        def worker(i):
            start.wait()
            try:
                self.inv.reserve("RICE", 1, "SO-%d" % i, k())
                wins.append(i)
            except StockError:
                losses.append(i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((len(wins), len(losses)), (10, 10))
        b = self.bal()
        self.assertEqual((b["reserved"], b["available"]), (10, 0))
        self.assertEqual(self.inv.check_ledger(), [])


if __name__ == "__main__":
    unittest.main()
