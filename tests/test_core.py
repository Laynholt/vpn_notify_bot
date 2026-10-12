import tempfile
import unittest
import sqlite3
from contextlib import closing
from pathlib import Path

from config import Settings
from storage import Storage, validate_field


FIELDS = dict(name="VPN", hoster="Host", website="https://example.com",
              ip_address="192.0.2.10", domain="vpn.example.com",
              starts_on="2026-10-01", ends_on="2026-10-19")


def create_legacy_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript("""
            CREATE TABLE servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, hoster TEXT NOT NULL, website TEXT NOT NULL,
                starts_on TEXT NOT NULL, ends_on TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE deliveries (
                server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                ends_on TEXT NOT NULL, admin_id INTEGER NOT NULL, day TEXT NOT NULL,
                PRIMARY KEY (server_id, ends_on, admin_id, day)
            );
            INSERT INTO servers VALUES (7, 'VPN', 'Host', 'https://example.com', '2026-10-01', '2026-10-19', 4);
            INSERT INTO deliveries VALUES (7, '2026-10-19', 123, '2026-10-12');
        """)


class ConfigTests(unittest.TestCase):
    def load(self, extra=""):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / ".env").write_text(
                "BOT_TOKEN=123456789:abcdefghijklmnopqrstuvwxyz012345678\n"
                "ADMIN_IDS=123,456\n" + extra, encoding="utf-8")
            return Settings.load(base)

    def test_defaults_and_relative_database(self):
        settings = self.load()
        self.assertEqual(settings.admin_ids, (123, 456))
        self.assertEqual(str(settings.notify_time), "12:00:00")
        self.assertEqual(settings.timezone.key, "Europe/Moscow")
        self.assertEqual(settings.remind_days, 7)
        self.assertTrue(settings.notify_overdue)
        self.assertIsNone(settings.proxy_url)
        self.assertTrue(settings.database_path.is_absolute())

    def test_invalid_settings_fail_without_secrets(self):
        for line in ("ADMIN_IDS=", "ADMIN_IDS=0", "ADMIN_IDS=abc", "REMIND_DAYS=-1",
                     "REMIND_DAYS=2.5", "NOTIFY_TIME=25:00", "NOTIFY_TIME=1:00",
                     "TIMEZONE=Missing/Zone", "NOTIFY_OVERDUE=yes", "BOT_TOKEN=secret",
                     "PROXY_URL=ftp://localhost:10", "PROXY_URL=http://localhost:bad"):
            with self.subTest(line=line), self.assertRaises(ValueError) as error:
                self.load(line + "\n")
            self.assertNotIn("secret", str(error.exception))

    def test_http_and_socks_proxy_are_explicit(self):
        for proxy in ("http://127.0.0.1:10808", "socks5://user:pass@localhost:10808"):
            self.assertEqual(self.load("PROXY_URL=" + proxy).proxy_url, proxy)
        self.assertIsNone(self.load("PROXY_URL=\n").proxy_url)


class ValidationTests(unittest.TestCase):
    def test_ip_accepts_ipv4_and_ipv6_but_rejects_urls_and_networks(self):
        for value, expected in (("192.0.2.10", "192.0.2.10"), ("2001:0db8::1", "2001:db8::1")):
            self.assertEqual(validate_field("ip_address", value), expected)
        for value in ("", "300.1.2.3", "example.com", "https://192.0.2.1", "192.0.2.1/24"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_field("ip_address", value)

    def test_domain_accepts_hostname_and_idn_but_rejects_url_and_ip(self):
        self.assertEqual(validate_field("domain", " VPN.Example.COM. "), "vpn.example.com")
        self.assertEqual(validate_field("domain", "пример.рф"), "xn--e1afmkfd.xn--p1ai")
        for value in ("", "https://example.com", "example.com/path", "192.0.2.10", "localhost",
                      "-vpn.example.com", "vpn-.example.com", "vpn..example.com", "a" * 64 + ".com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_field("domain", value)

    def test_date_is_strict_and_real(self):
        self.assertEqual(validate_field("ends_on", "29.02.2028"), "2028-02-29")
        for value in ("29.02.2027", "31.04.2026", "1.2.2026", "2026-10-19", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_field("ends_on", value)

    def test_site_rejects_non_http_and_unsafe_urls(self):
        for value in ("javascript:alert(1)", "ftp://example.com", "https://",
                      "https://user:password@example.com", "https://example.com/a b",
                      "https://example.com:bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_field("website", value)
        self.assertEqual(validate_field("website", " https://example.com "), "https://example.com")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "data" / "db.sqlite3"
        self.store = Storage(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_persists_records_and_deliveries(self):
        server = self.store.add(FIELDS)
        self.store.mark_delivered(server["id"], "2026-10-19", 123, "2026-10-12")
        self.store.close()
        self.store = Storage(self.path)
        self.assertEqual(self.store.get(server["id"])["name"], "VPN")
        self.assertEqual(self.store.get(server["id"])["ip_address"], "192.0.2.10")
        self.assertEqual(self.store.get(server["id"])["domain"], "vpn.example.com")
        self.assertTrue(self.store.delivered(server["id"], "2026-10-19", 123, "2026-10-12"))
        self.assertFalse(self.store.delivered(server["id"], "2026-10-19", 456, "2026-10-12"))

    def test_stale_edit_and_delete_cannot_change_renewed_server(self):
        server = self.store.add(FIELDS)
        self.assertTrue(self.store.update(server["id"], 1, {"ends_on": "2026-11-19"}))
        self.assertFalse(self.store.update(server["id"], 1, {"name": "Old"}))
        self.assertFalse(self.store.delete(server["id"], 1))
        self.assertEqual(self.store.get(server["id"])["ends_on"], "2026-11-19")
        self.assertTrue(self.store.delete(server["id"], 2))
        self.assertIsNone(self.store.get(server["id"]))

    def test_invalid_period_does_not_destroy_saved_data(self):
        server = self.store.add(FIELDS)
        with self.assertRaises(ValueError):
            self.store.update(server["id"], 1, {"ends_on": "2026-09-01"})
        self.assertEqual(self.store.get(server["id"])["version"], 1)
        with self.assertRaises(ValueError):
            self.store.add({**FIELDS, "name": ""})

    def test_deleted_server_ids_are_not_reused(self):
        old = self.store.add(FIELDS)
        self.store.delete(old["id"], 1)
        self.assertGreater(self.store.add(FIELDS)["id"], old["id"])

    def test_new_server_requires_both_address_fields(self):
        for key in ("ip_address", "domain"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.add({**FIELDS, key: ""})
        self.assertEqual(self.store.list(), [])


class MigrationTests(unittest.TestCase):
    def test_old_database_gains_empty_fields_and_keeps_delivery_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            create_legacy_database(path)
            for _ in range(2):
                store = Storage(path)
                try:
                    self.assertEqual(store.get(7), dict(id=7, version=4, name="VPN", hoster="Host",
                        website="https://example.com", starts_on="2026-10-01", ends_on="2026-10-19",
                        ip_address="", domain="", deleted_at=None, payment_starts_on="2026-10-01",
                        amount_kopeks=None, total_paid_kopeks=0, known_payments=0, unknown_payments=1))
                    self.assertEqual(len(store.list()), 1)
                    self.assertTrue(store.delivered(7, "2026-10-19", 123, "2026-10-12"))
                finally:
                    store.close()

    def test_legacy_server_can_be_renewed_then_addresses_filled_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            create_legacy_database(path)
            store = Storage(path)
            try:
                self.assertTrue(store.update(7, 4, {"ends_on": "2026-11-19"}))
                self.assertTrue(store.update(7, 5, {"ip_address": "192.0.2.10"}))
                self.assertEqual(store.get(7)["domain"], "")
                self.assertTrue(store.update(7, 6, {"domain": "vpn.example.com"}))
            finally:
                store.close()
            store = Storage(path)
            try:
                self.assertEqual(store.get(7)["ip_address"], "192.0.2.10")
                self.assertEqual(store.get(7)["domain"], "vpn.example.com")
                self.assertEqual(store.get(7)["ends_on"], "2026-11-19")
                self.assertTrue(store.delivered(7, "2026-10-19", 123, "2026-10-12"))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
