import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

from config import Settings
from helpers import telegram_bot
from reminders import Notifier, reminder_keyboard, server_text
from storage import Storage
from test_core import FIELDS


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        (base / ".env").write_text("BOT_TOKEN=123:abc\nADMIN_IDS=123,456", encoding="utf-8")
        self.settings = Settings.load(base)
        self.store = Storage(self.settings.database_path)
        self.bot, self.session = telegram_bot()
        self.notifier = Notifier(self.bot, self.store, self.settings)
        self.pause = patch("reminders.asyncio.sleep", new=AsyncMock())
        self.pause.start()

    async def asyncTearDown(self):
        self.pause.stop()
        await self.bot.session.close()
        self.store.close()
        self.directory.cleanup()

    async def tick(self, day=12, hour=9):
        await self.notifier.tick(datetime(2026, 10, day, hour, tzinfo=timezone.utc))

    async def test_local_time_and_seven_day_boundary(self):
        server = self.store.add(FIELDS)
        await self.tick(hour=8)
        self.assertEqual(self.session.messages, [])
        await self.tick()
        self.assertEqual([msg.chat_id for msg in self.session.messages], [123, 456])
        self.assertIn("7", self.session.messages[0].text)
        self.assertEqual(len(self.session.messages[0].reply_markup.inline_keyboard), 1)
        self.assertTrue(self.store.delivered(server["id"], "2026-10-19", 123, "2026-10-12"))

    async def test_eight_days_away_is_not_due(self):
        self.store.add({**FIELDS, "ends_on": "2026-10-20"})
        await self.tick()
        self.assertEqual(self.session.messages, [])

    async def test_restart_does_not_duplicate_but_next_day_sends(self):
        self.store.add(FIELDS)
        await self.tick()
        self.notifier = Notifier(self.bot, self.store, self.settings)
        await self.tick(hour=15)
        self.assertEqual(len(self.session.messages), 2)
        await self.tick(day=13)
        self.assertEqual(len(self.session.messages), 4)

    async def test_overdue_and_last_day(self):
        self.store.add({**FIELDS, "ends_on": "2026-10-11"})
        await self.tick()
        self.assertIn("Просрочен", self.session.messages[0].text)
        buttons = self.session.messages[0].reply_markup.inline_keyboard
        self.assertEqual(buttons[1][0].text, "Удалить")
        self.notifier = Notifier(self.bot, self.store, replace(self.settings, notify_overdue=False))
        self.session.requests.clear()
        await self.tick(day=13)
        self.assertEqual(self.session.messages, [])
        self.store.add({**FIELDS, "ends_on": "2026-10-13"})
        await self.tick(day=13)
        self.assertEqual(len(self.session.messages), 2)

    async def test_failed_recipient_does_not_block_other_and_can_retry(self):
        server = self.store.add(FIELDS)
        self.session.failures[123] = lambda method: TelegramNetworkError(method, "offline")
        with self.assertLogs("reminders", level="WARNING"):
            await self.tick()
        self.assertTrue(self.store.delivered(server["id"], server["ends_on"], 456, "2026-10-12"))
        self.assertFalse(self.store.delivered(server["id"], server["ends_on"], 123, "2026-10-12"))
        del self.session.failures[123]
        await self.tick(hour=10)
        self.assertTrue(self.store.delivered(server["id"], server["ends_on"], 123, "2026-10-12"))
        self.assertEqual(sum(msg.chat_id == 456 for msg in self.session.messages), 1)

    async def test_retry_after_is_honored_without_blocking_second_admin(self):
        self.store.add(FIELDS)
        self.session.failures[123] = lambda method: TelegramRetryAfter(method, "flood", 120)
        with self.assertLogs("reminders", level="WARNING"):
            await self.tick()
        del self.session.failures[123]
        await self.notifier.tick(datetime(2026, 10, 12, 9, 1, tzinfo=timezone.utc))
        self.assertEqual(len(self.session.messages), 2)
        await self.notifier.tick(datetime(2026, 10, 12, 9, 3, tzinfo=timezone.utc))
        self.assertEqual(len(self.session.messages), 3)

    async def test_new_server_after_schedule_and_new_period_are_checked(self):
        await self.tick()
        server = self.store.add({**FIELDS, "ends_on": "2026-10-12"})
        await self.tick(hour=10)
        self.assertEqual(len(self.session.messages), 2)
        self.store.update(server["id"], 1, {"ends_on": "2026-11-12"})
        await self.tick(hour=11)
        self.assertEqual(len(self.session.messages), 2)

    async def test_html_in_name_is_escaped(self):
        server = self.store.add({**FIELDS, "name": "<b>VPN</b>"})
        self.assertIn("&lt;b&gt;VPN&lt;/b&gt;", server_text(server))
        self.assertNotIn("<b>VPN</b>", server_text(server))


if __name__ == "__main__":
    unittest.main()
