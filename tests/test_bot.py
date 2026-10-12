import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot import create_dispatcher
from config import Settings
from helpers import telegram_bot
from storage import Storage
from test_core import FIELDS, create_legacy_database


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        (base / ".env").write_text("BOT_TOKEN=123:abc\nADMIN_IDS=123,456", encoding="utf-8")
        self.settings = Settings.load(base)
        self.store = Storage(self.settings.database_path)
        self.bot, self.session = telegram_bot()
        self.dispatcher = create_dispatcher(self.store, self.settings)
        self.update_id = 0
        self.clock = patch("bot.BotUI.today", return_value=date(2026, 10, 12))
        self.clock.start()

    async def asyncTearDown(self):
        self.clock.stop()
        await self.dispatcher.fsm.close()
        await self.bot.session.close()
        self.store.close()
        self.directory.cleanup()

    async def send(self, text, admin=123, chat_type="private"):
        self.update_id += 1
        message = Message(message_id=self.update_id, date=datetime.now(timezone.utc),
                          chat=Chat(id=admin, type=chat_type),
                          from_user=User(id=admin, is_bot=False, first_name="Admin"), text=text)
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.update_id, message=message))

    async def click(self, data, admin=123, chat_type="private"):
        self.update_id += 1
        message = Message(message_id=self.update_id, date=datetime.now(timezone.utc),
                          chat=Chat(id=admin, type=chat_type),
                          from_user=User(id=self.bot.id, is_bot=True, first_name="Bot"), text="Buttons")
        callback = CallbackQuery(id=str(self.update_id), from_user=User(id=admin, is_bot=False,
                                  first_name="Admin"), chat_instance="test", data=data, message=message)
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.update_id, callback_query=callback))

    def button(self, text):
        for row in self.session.messages[-1].reply_markup.inline_keyboard:
            for button in row:
                if button.text == text:
                    return button.callback_data
        self.fail(f"No button {text}")

    async def test_unauthorized_messages_and_callbacks_are_silently_ignored(self):
        await self.send("/start", admin=999)
        await self.click("add", admin=999)
        await self.send("/start", chat_type="group")
        await self.click("add", chat_type="group")
        self.assertEqual(self.session.requests, [])
        self.assertEqual(self.store.list(), [])

    async def test_add_saves_only_after_confirmation(self):
        await self.click("add")
        for text in ("VPN", "Host", "https://example.com", "192.0.2.10", "vpn.example.com", "01.10.2026", "19.10.2026"):
            await self.send(text)
        self.assertEqual(self.store.list(), [])
        await self.click(self.button("Сохранить"))
        self.assertEqual(self.store.list()[0]["ends_on"], "2026-10-19")
        self.assertIn("192.0.2.10", self.session.messages[-1].text)
        self.assertIn("vpn.example.com", self.session.messages[-1].text)

    async def test_invalid_input_keeps_step_and_period_is_validated(self):
        await self.click("add")
        await self.send("")
        self.assertIn("название", self.session.messages[-1].text.lower())
        for text in ("VPN", "Host", "javascript:alert(1)", "https://example.com",
                     "bad IP", "192.0.2.10", "https://vpn.example.com", "vpn.example.com", "19.10.2026"):
            await self.send(text)
        await self.send("18.10.2026")
        self.assertIn("раньше", self.session.messages[-1].text)
        await self.send("20.10.2026")
        await self.click(self.button("Сохранить"))
        self.assertEqual(self.store.list()[0]["ends_on"], "2026-10-20")

    async def test_word_then_valid_site_preserves_all_previously_entered_fields(self):
        await self.click("add")
        await self.send("First server")
        await self.send("My hoster")
        await self.send("hoster")
        await self.send("https://example.com")
        self.assertIn("IP", self.session.messages[-1].text)
        await self.send("192.0.2.10")
        await self.send("vpn.example.com")
        await self.send("01.10.2026")
        await self.send("19.10.2026")
        await self.click(self.button("Сохранить"))
        server = self.store.list()[0]
        self.assertEqual(server["name"], "First server")
        self.assertEqual(server["hoster"], "My hoster")
        self.assertEqual(server["website"], "https://example.com")

    async def test_legacy_server_can_renew_and_edit_new_fields_separately(self):
        await self.dispatcher.fsm.close()
        self.store.close()
        self.settings.database_path.unlink()
        create_legacy_database(self.settings.database_path)
        self.store = Storage(self.settings.database_path)
        self.dispatcher = create_dispatcher(self.store, self.settings)
        await self.click("view:7")
        self.assertIn("IP-адрес: не указан", self.session.messages[-1].text)
        self.assertIn("Домен: не указан", self.session.messages[-1].text)
        await self.click("renew:7:4")
        await self.send("19.11.2026")
        await self.click(self.button("Сохранить"))
        self.assertEqual(self.store.get(7)["ends_on"], "2026-11-19")
        await self.click("edit:7:5:ip_address")
        await self.send("invalid")
        await self.send("2001:db8::1")
        await self.click(self.button("Сохранить"))
        self.assertEqual(self.store.get(7)["domain"], "")
        await self.click("edit:7:6:domain")
        await self.send("vpn.example.com")
        await self.click(self.button("Сохранить"))
        self.assertEqual(self.store.get(7)["ip_address"], "2001:db8::1")
        self.assertEqual(self.store.get(7)["domain"], "vpn.example.com")

    async def test_my_servers_menu_lists_dates_and_opens_card(self):
        server = self.store.add({**FIELDS, "name": "<VPN>"})
        await self.send("/start")
        actions = [button.callback_data for row in self.session.messages[-1].reply_markup.inline_keyboard
                   for button in row]
        self.assertIn("servers:0", actions)
        await self.click("servers:0")
        message = self.session.messages[-1]
        self.assertIn("&lt;VPN&gt;", message.text)
        self.assertIn("19.10.2026", message.text)
        await self.click(message.reply_markup.inline_keyboard[0][0].callback_data)
        self.assertIn("https://example.com", self.session.messages[-1].text)
        self.assertEqual(self.store.get(server["id"])["version"], 1)

    async def test_empty_edit_list_only_offers_add_and_main_menu(self):
        await self.click("list:0")
        actions = [button.callback_data for row in self.session.messages[-1].reply_markup.inline_keyboard
                   for button in row]
        self.assertEqual(actions, ["add", "menu"])

    async def test_my_servers_pagination_preserves_overview_and_telegram_length(self):
        for index in range(11):
            self.store.add({**FIELDS, "name": '"' * 90 + str(index)})
        await self.click("servers:0")
        message = self.session.messages[-1]
        self.assertLessEqual(len(message.text), 4096)
        actions = [button.callback_data for row in message.reply_markup.inline_keyboard for button in row]
        self.assertIn("servers:1", actions)
        await self.click("servers:1")
        self.assertIn("19.10.2026", self.session.messages[-1].text)

    async def test_my_servers_sorts_expiry_globally_before_pagination(self):
        later = self.store.add({**FIELDS, "name": "Later", "ends_on": "2026-12-01"})
        equal_expiry = []
        for index in range(9):
            equal_expiry.append(self.store.add({**FIELDS, "name": f"Soon {index}", "ends_on": "2026-10-13"}))
        overdue = self.store.add({**FIELDS, "name": "Overdue", "ends_on": "2026-10-10"})
        await self.click("servers:0")
        actions = [button.callback_data for row in self.session.messages[-1].reply_markup.inline_keyboard
                   for button in row if button.callback_data.startswith("view:")]
        self.assertEqual(actions, [f"view:{overdue['id']}"] + [f"view:{row['id']}" for row in equal_expiry])
        await self.click("servers:1")
        actions = [button.callback_data for row in self.session.messages[-1].reply_markup.inline_keyboard
                   for button in row if button.callback_data.startswith("view:")]
        self.assertEqual(actions, [f"view:{later['id']}"])

    async def test_overview_text_sorts_by_expiry_but_buttons_by_alphabet_without_flags(self):
        alpha = self.store.add({**FIELDS, "name": "🇺🇸 Альфа", "ends_on": "2026-12-01"})
        last = self.store.add({**FIELDS, "name": "🇩🇪 Яблоко", "ends_on": "2026-10-13"})
        beta = self.store.add({**FIELDS, "name": "🇨🇦 бета", "ends_on": "2026-11-01"})
        keycap = self.store.add({**FIELDS, "name": "1️⃣ Ясень", "ends_on": "2026-12-02"})
        await self.click("servers:0")
        message = self.session.messages[-1]
        self.assertLess(message.text.index("Яблоко"), message.text.index("бета"))
        self.assertLess(message.text.index("бета"), message.text.index("Альфа"))
        actions = [button.callback_data for row in message.reply_markup.inline_keyboard
                   for button in row if button.callback_data.startswith("view:")]
        self.assertEqual(actions, [f"view:{alpha['id']}", f"view:{beta['id']}", f"view:{last['id']}", f"view:{keycap['id']}"])

    async def test_cancel_and_abandoned_renewal_leave_end_date_unchanged(self):
        server = self.store.add(FIELDS)
        await self.click(f"renew:{server['id']}:1")
        self.assertEqual(self.store.get(server["id"])["ends_on"], "2026-10-19")
        await self.send("bad date")
        await self.send("/cancel")
        self.assertEqual(self.store.get(server["id"])["version"], 1)
        await self.send("19.11.2026")
        self.assertEqual(self.store.get(server["id"])["ends_on"], "2026-10-19")

    async def test_renewal_validates_and_conflicting_admin_cannot_overwrite(self):
        server = self.store.add(FIELDS)
        await self.click(f"renew:{server['id']}:1", admin=456)
        await self.send("19.12.2026", admin=456)
        second_save = self.button("Сохранить")
        await self.click(f"renew:{server['id']}:1")
        await self.send("19.10.2026")
        self.assertIn("позже", self.session.messages[-1].text)
        await self.send("19.11.2026")
        await self.click(self.button("Сохранить"))
        await self.click(second_save, admin=456)
        self.assertEqual(self.store.get(server["id"])["ends_on"], "2026-11-19")
        self.assertIn("изменён", self.session.messages[-1].text)

    async def test_delete_requires_confirmation_and_stale_delete_is_rejected(self):
        server = self.store.add(FIELDS)
        await self.click(f"delete:{server['id']}:1")
        delete = self.button("Да, удалить")
        self.assertIsNotNone(self.store.get(server["id"]))
        self.store.update(server["id"], 1, {"ends_on": "2026-11-19"})
        await self.click(delete)
        self.assertIsNotNone(self.store.get(server["id"]))
        await self.click(f"delete:{server['id']}:2")
        await self.click(self.button("Да, удалить"))
        self.assertIsNone(self.store.get(server["id"]))

    async def test_old_save_and_cancel_do_not_affect_new_draft(self):
        server = self.store.add(FIELDS)
        await self.click(f"renew:{server['id']}:1")
        old_cancel = self.button("Отмена")
        await self.send("19.11.2026")
        old_save = self.button("Сохранить")
        await self.click(f"renew:{server['id']}:1")
        await self.send("19.12.2026")
        new_save = self.button("Сохранить")
        await self.click(old_cancel)
        await self.click(old_save)
        self.assertEqual(self.store.get(server["id"])["ends_on"], "2026-10-19")
        await self.click(new_save)
        self.assertEqual(self.store.get(server["id"])["ends_on"], "2026-12-19")

    async def test_edit_field_and_empty_list(self):
        await self.click("list:0")
        self.assertIn("нет", self.session.messages[-1].text)
        server = self.store.add(FIELDS)
        await self.click(f"edit:{server['id']}:1:name")
        await self.send("<VPN>")
        await self.click(self.button("Сохранить"))
        self.assertEqual(self.store.get(server["id"])["name"], "<VPN>")
        self.assertIn("&lt;VPN&gt;", self.session.messages[-1].text)


if __name__ == "__main__":
    unittest.main()
