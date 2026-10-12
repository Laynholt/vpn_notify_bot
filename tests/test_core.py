import tempfile
import unittest
from pathlib import Path

from config import Settings
from storage import Storage, validate_field


FIELDS = dict(name="VPN", hoster="Host", website="https://example.com",
              starts_on="2026-10-01", ends_on="2026-10-19")


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


if __name__ == "__main__":
    unittest.main()
