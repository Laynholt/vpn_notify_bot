import sqlite3
import tempfile
import unittest
from datetime import date
from contextlib import closing
from pathlib import Path

import storage
from storage import Storage, validate_field
from test_core import FIELDS, create_legacy_database


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "history.sqlite3"
        self.store = Storage(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_money_is_exact_and_rejects_invalid_values(self):
        for text, expected in (("1500,50", 150050), ("1500.50", 150050), ("0", 0), ("0.01", 1)):
            self.assertEqual(validate_field("amount_kopeks", text), expected)
        self.assertEqual(storage.format_money(150050), "1 500,50 ₽")
        self.assertEqual(storage.format_money(100000050), "1 000 000,50 ₽")
        self.assertEqual(storage.format_money(None), "не указано")
        for text in ("-1", "1.001", "NaN", "1e5", "1,2.3", "9" * 100, ""):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_field("amount_kopeks", text)

    def test_renew_appends_and_edit_does_not_append(self):
        server = self.store.add({**FIELDS, "amount_kopeks": 10000})
        self.assertTrue(self.store.renew(server["id"], 1, "2026-11-19", 20000))
        history = self.store.payment_history(server["id"])
        self.assertEqual([(p["starts_on"], p["ends_on"], p["amount_kopeks"]) for p in history],
                         [("2026-10-01", "2026-10-19", 10000), ("2026-10-20", "2026-11-19", 20000)])
        saved = self.store.get(server["id"])
        self.assertEqual(saved["starts_on"], "2026-10-01")
        self.assertEqual(saved["total_paid_kopeks"], 30000)
        self.assertEqual(saved["payment_starts_on"], "2026-10-20")
        self.assertTrue(self.store.update(server["id"], 2, {"amount_kopeks": 20050}))
        self.assertEqual(self.store.get(server["id"])["total_paid_kopeks"], 30050)
        self.assertEqual(len(self.store.payment_history(server["id"])), 2)

    def test_archive_keeps_history_and_rejects_stale_writes(self):
        server = self.store.add({**FIELDS, "amount_kopeks": 10000})
        self.assertTrue(self.store.delete(server["id"], 1))
        self.assertIsNone(self.store.get(server["id"]))
        self.assertEqual(self.store.list(), [])
        self.assertEqual(len(self.store.payment_history(server["id"])), 1)
        archived = self.store.statistics(server["id"])[0]
        self.assertTrue(archived["deleted_at"])
        self.assertEqual(archived["total_paid_kopeks"], 10000)
        self.assertEqual(len(archived["payments"]), 1)
        self.assertFalse(self.store.renew(server["id"], 2, "2026-11-19", 20000))
        self.assertFalse(self.store.update(server["id"], 2, {"name": "Changed"}))
        self.assertFalse(self.store.delete(server["id"], 2))
        self.store.mark_delivered(server["id"], server["ends_on"], 123, "2026-10-12")
        self.assertFalse(self.store.delivered(server["id"], server["ends_on"], 123, "2026-10-12"))
        self.assertEqual(self.store.statistics(999), [])

    def test_scheduled_archive_survives_restart_and_keeps_inclusive_paid_day(self):
        server = self.store.add({**FIELDS, "amount_kopeks": 10000})
        history = self.store.payment_history(server["id"])
        self.store.mark_delivered(server["id"], server["ends_on"], 123, "2026-10-12")
        self.assertTrue(self.store.schedule_archive(server["id"], 1))
        self.assertFalse(self.store.schedule_archive(server["id"], 1))
        self.store.close()
        self.store = Storage(self.path)
        self.assertTrue(self.store.get(server["id"])["archive_pending"])
        self.store.archive_due(date(2026, 10, 19))
        self.assertIsNotNone(self.store.get(server["id"]))
        self.store.archive_due(date(2026, 10, 20))
        archived = self.store.statistics(server["id"])[0]
        self.assertIsNone(self.store.get(server["id"]))
        self.assertTrue(archived["deleted_at"])
        self.assertEqual(archived["payments"], history)
        self.assertEqual(archived["total_paid_kopeks"], 10000)
        self.assertTrue(self.store.delivered(server["id"], server["ends_on"], 123, "2026-10-12"))
        self.store.archive_due(date(2026, 10, 21))
        self.assertEqual(self.store.statistics(server["id"])[0], archived)

    def test_renewal_cancels_pending_archive_but_field_edit_keeps_it(self):
        server = self.store.add(FIELDS)
        self.store.schedule_archive(server["id"], 1)
        self.assertTrue(self.store.update(server["id"], 2, {"name": "Changed"}))
        self.assertTrue(self.store.get(server["id"])["archive_pending"])
        self.assertFalse(self.store.renew(server["id"], 1, "2026-11-19", 10000))
        self.assertTrue(self.store.get(server["id"])["archive_pending"])
        self.assertTrue(self.store.renew(server["id"], 3, "2026-11-19", 10000))
        self.assertFalse(self.store.get(server["id"])["archive_pending"])
        self.store.archive_due(date(2026, 12, 1))
        self.assertIsNotNone(self.store.get(server["id"]))

    def test_migration_twice_keeps_one_unknown_payment_and_deliveries(self):
        self.store.close()
        self.path.unlink()
        create_legacy_database(self.path)
        for _ in range(2):
            self.store = Storage(self.path)
            saved = self.store.get(7)
            self.assertEqual(saved["version"], 4)
            self.assertEqual(saved["unknown_payments"], 1)
            self.assertEqual(saved["known_payments"], 0)
            self.assertIsNone(saved["amount_kopeks"])
            self.assertEqual(len(self.store.payment_history(7)), 1)
            self.assertTrue(self.store.delivered(7, "2026-10-19", 123, "2026-10-12"))
            self.store.close()
        self.store = Storage(self.path)

    def test_unknown_amount_and_free_renewal_are_distinct(self):
        server = self.store.add(FIELDS)
        self.store.renew(server["id"], 1, "2026-11-19", 0)
        saved = self.store.get(server["id"])
        self.assertEqual(saved["amount_kopeks"], 0)
        self.assertEqual(saved["total_paid_kopeks"], 0)
        self.assertEqual((saved["known_payments"], saved["unknown_payments"]), (1, 1))
        self.assertIsNone(self.store.payment_history(server["id"])[0]["amount_kopeks"])

    def test_stale_renew_and_invalid_boundaries_leave_history_unchanged(self):
        server = self.store.add({**FIELDS, "amount_kopeks": 10000})
        self.store.renew(server["id"], 1, "2026-11-19", 20000)
        before = self.store.statistics(server["id"])
        self.assertFalse(self.store.renew(server["id"], 1, "2026-12-19", 30000))
        for fields in ({"starts_on": "2026-10-20"}, {"ends_on": "2026-10-19"}, {"amount_kopeks": -1}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.store.update(server["id"], 2, fields)
        self.assertEqual(self.store.statistics(server["id"]), before)

    def test_renewal_rollback_keeps_dates_and_payment_together(self):
        server = self.store.add({**FIELDS, "amount_kopeks": 10000})
        self.store.connection.execute("""CREATE TRIGGER fail_payment BEFORE INSERT ON payments
            BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.renew(server["id"], 1, "2026-11-19", 20000)
        saved = self.store.get(server["id"])
        self.assertEqual((saved["ends_on"], saved["version"], saved["total_paid_kopeks"]),
                         ("2026-10-19", 1, 10000))
        self.assertEqual(len(self.store.payment_history(server["id"])), 1)

    def test_maximum_date_and_non_integer_amount_are_rejected(self):
        server = self.store.add({**FIELDS, "ends_on": "9999-12-31", "amount_kopeks": 0})
        with self.assertRaises(ValueError):
            self.store.renew(server["id"], 1, "9999-12-31", 10000)
        for amount in (1.5, True, -1, 10**30):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.store.add({**FIELDS, "amount_kopeks": amount})

    def test_bonus_migration_and_updates_keep_correct_annotation(self):
        server = self.store.add({**FIELDS, "amount_kopeks": 0})
        for _ in range(2):
            self.store.close()
            self.store = Storage(self.path)
            self.assertEqual(self.store.get(server["id"])["paid_with_bonus"], 0)
            self.assertEqual(len(self.store.payment_history(server["id"])), 1)
        with self.store.connection:
            self.store.connection.execute("UPDATE payments SET paid_with_bonus=1 WHERE server_id=?", (server["id"],))
        saved = self.store.get(server["id"])
        self.store.update(server["id"], 1, {**saved, "name": "New name"})
        self.assertEqual(self.store.get(server["id"])["paid_with_bonus"], 1)
        self.store.update(server["id"], 2, {"amount_kopeks": 10000})
        self.assertEqual(self.store.get(server["id"])["paid_with_bonus"], 0)
        self.store.renew(server["id"], 3, "2026-11-19", 0)
        self.assertEqual(self.store.payment_history(server["id"])[-1]["paid_with_bonus"], 0)

    def test_bonus_migration_preserves_existing_payment_table(self):
        self.store.close()
        self.path.unlink()
        create_legacy_database(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript("""CREATE TABLE payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER NOT NULL REFERENCES servers(id),
                starts_on TEXT NOT NULL, ends_on TEXT NOT NULL, amount_kopeks INTEGER);
                INSERT INTO payments VALUES (12, 7, '2026-10-01', '2026-10-19', 150050);""")
        for _ in range(2):
            self.store = Storage(self.path)
            history = self.store.payment_history(7)
            self.assertEqual(len(history), 1)
            self.assertEqual((history[0]["id"], history[0]["amount_kopeks"], history[0].get("paid_with_bonus")),
                             (12, 150050, 0))
            self.assertEqual(self.store.get(7)["version"], 4)
            self.assertTrue(self.store.delivered(7, "2026-10-19", 123, "2026-10-12"))
            self.store.close()
        self.store = Storage(self.path)


if __name__ == "__main__":
    unittest.main()
