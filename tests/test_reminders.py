import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, time, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

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
        self.assertIn("192.0.2.10", self.session.messages[0].text)
        self.assertIn("vpn.example.com", self.session.messages[0].text)
        actions = [button.callback_data for row in self.session.messages[0].reply_markup.inline_keyboard
                   for button in row if button.callback_data]
        self.assertFalse(any(action.startswith("delete:") for action in actions))
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
        self.assertTrue(any(button.callback_data and button.callback_data.startswith("delete:")
                            for row in buttons for button in row))
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

    async def test_retry_after_starts_when_error_arrives_in_slow_batch(self):
        self.store.add(FIELDS)
        self.session.failures[123] = lambda method: TelegramRetryAfter(method, "flood", 120)
        # Ошибка возникает спустя 61 секунду после начала проверки.
        clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 61]))
        with patch("reminders.time", clock, create=True), self.assertLogs("reminders", level="WARNING"):
            await self.tick()
        del self.session.failures[123]
        await self.notifier.tick(datetime(2026, 10, 12, 9, 2, 30, tzinfo=timezone.utc))
        self.assertEqual(len(self.session.messages), 2)
        await self.notifier.tick(datetime(2026, 10, 12, 9, 4, tzinfo=timezone.utc))
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

    async def test_due_and_overdue_notifications_link_to_hoster(self):
        server = self.store.add(FIELDS)
        for day in (12, 20):
            with self.subTest(day=day):
                await self.tick(day=day)
                buttons = [button for row in self.session.messages[-1].reply_markup.inline_keyboard
                           for button in row]
                links = [button for button in buttons if button.url]
                self.assertEqual([button.url for button in links], ["https://example.com"])
                self.assertTrue(any(button.callback_data == f"renew:{server['id']}:1" for button in buttons))

    async def test_midnight_uses_local_calendar_day(self):
        server = self.store.add(FIELDS)
        settings = replace(self.settings, notify_time=time(0, 0))
        notifier = Notifier(self.bot, self.store, settings)
        await notifier.tick(datetime(2026, 10, 12, 21, 1, tzinfo=timezone.utc))
        self.assertTrue(self.store.delivered(server["id"], server["ends_on"], 123, "2026-10-13"))
        self.assertFalse(self.store.delivered(server["id"], server["ends_on"], 123, "2026-10-12"))

    async def test_dst_repeated_hour_does_not_duplicate_delivery(self):
        self.store.add({**FIELDS, "ends_on": "2026-11-07"})
        settings = replace(self.settings, notify_time=time(1, 30), timezone=ZoneInfo("America/New_York"))
        notifier = Notifier(self.bot, self.store, settings)
        await notifier.tick(datetime(2026, 11, 1, 5, 35, tzinfo=timezone.utc))
        await notifier.tick(datetime(2026, 11, 1, 6, 35, tzinfo=timezone.utc))
        self.assertEqual(len(self.session.messages), 2)

    async def test_dst_missing_hour_sends_after_clock_jumps_forward(self):
        self.store.add({**FIELDS, "starts_on": "2026-03-01", "ends_on": "2026-04-05"})
        settings = replace(self.settings, notify_time=time(2, 30), timezone=ZoneInfo("Europe/Berlin"))
        notifier = Notifier(self.bot, self.store, settings)
        await notifier.tick(datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc))
        self.assertEqual(self.session.messages, [])
        await notifier.tick(datetime(2026, 3, 29, 1, 5, tzinfo=timezone.utc))
        self.assertEqual(len(self.session.messages), 2)


if __name__ == "__main__":
    unittest.main()
