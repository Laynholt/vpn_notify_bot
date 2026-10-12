import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from secrets import token_hex

from aiogram import BaseMiddleware, Dispatcher, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from reminders import display_date, server_text
from reports import build_report
from storage import FIELDS, format_money, validate_amount, validate_field, validate_payment_dates, validate_server


LABELS = {"name": "Название сервера", "hoster": "Название хостера", "website": "Сайт хостера",
          "ip_address": "IP-адрес", "domain": "Доменное имя",
          "starts_on": "Дата начала аренды", "ends_on": "Дата окончания оплаты",
          "amount_kopeks": "Стоимость последнего периода"}
FORM_FIELDS = (*FIELDS, "amount_kopeks")
logger = logging.getLogger(__name__)


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows])


MENU = keyboard([[('➕ Добавить сервер', 'add')], [('✏️ Изменить сервер', 'list:0')],
                 [('🖥 Мои сервера', 'servers:0')], [('📊 Статистика', 'stats:0')]])


class AdminOnly(BaseMiddleware):
    def __init__(self, admin_ids):
        self.admin_ids = admin_ids

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if (event.from_user and event.from_user.id in self.admin_ids
                and isinstance(message, Message) and message.chat.type == "private"
                and message.chat.id == event.from_user.id):
            return await handler(event, data)


@dataclass
class Draft:
    kind: str
    token: str = field(default_factory=lambda: token_hex(8))
    values: dict = field(default_factory=dict)
    server_id: int | None = None
    version: int | None = None
    selected_field: str | None = None
    ready: bool = False
    needs_prompt: bool = False


class BotUI:
    def __init__(self, storage, settings):
        self.storage, self.settings = storage, settings
        self.drafts = {}
        self.panels = {}
        self.panel_markups = {}

    def today(self):
        return datetime.now(self.settings.timezone).date()

    async def say(self, message, text, markup=None):
        if markup is None:
            markup = self.panel_markups.get(message.chat.id)
        panel_id = self.panels.get(message.chat.id)
        if panel_id is not None:
            try:
                await message.bot.edit_message_text(text, chat_id=message.chat.id, message_id=panel_id,
                    reply_markup=markup, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
                self.panel_markups[message.chat.id] = markup
                return
            except TelegramBadRequest as error:
                reason = error.message.lower()
                if "message is not modified" in reason:
                    return
                if "message to edit not found" not in reason and "message can't be edited" not in reason:
                    raise
        panel = await message.answer(text, reply_markup=markup, parse_mode="HTML",
                                     link_preview_options=LinkPreviewOptions(is_disabled=True))
        self.panels[message.chat.id] = panel.message_id
        self.panel_markups[message.chat.id] = markup

    async def menu(self, message, admin_id):
        self.drafts.pop(admin_id, None)
        await self.say(message, "🏠 Главное меню\nУправление оплатой серверов", MENU)

    async def list_servers(self, message, page, overview=False):
        servers = self.storage.list()
        if overview:
            servers.sort(key=lambda server: server["ends_on"])
        if not servers:
            await self.say(message, "У вас пока нет серверов. Добавим первый?",
                           keyboard([[("➕ Добавить сервер", "add")], [("🏠 Главное меню", "menu")]]))
            return
        page = min(max(page, 0), (len(servers) - 1) // 10)
        page_servers = servers[page * 10:(page + 1) * 10]
        action = "servers" if overview else "list"
        buttons = [(item["name"][:30], f"view:{item['id']}")
                   for item in sorted(page_servers, key=lambda server: re.sub(r"^(?:[0-9]\ufe0f?\u20e3|\W)+", "", server["name"]).casefold())]
        rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        navigation = []
        if page:
            navigation.append(("← Назад", f"{action}:{page - 1}"))
        if (page + 1) * 10 < len(servers):
            navigation.append(("Далее →", f"{action}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([("🏠 Главное меню", "menu")])
        if overview:
            lines = []
            for server in page_servers:
                remaining = (date.fromisoformat(server["ends_on"]) - self.today()).days
                status = f"осталось {remaining} дн." if remaining >= 0 else f"просрочен на {-remaining} дн."
                lines.append(f"<b>{escape(server['name'][:50])}</b> — до {display_date(server['ends_on'])}, {status}")
            text = f"🖥 Мои сервера (страница {page + 1})\n\n" + "\n\n".join(lines)
        else:
            text = f"✏️ Выберите сервер для изменения (страница {page + 1}):"
        await self.say(message, text, keyboard(rows))

    async def card(self, message, server_id):
        server = self.storage.get(server_id)
        if server is None:
            await self.say(message, "Сервер уже удалён.", MENU)
            return
        identity = f"{server['id']}:{server['version']}"
        rows = [[("Продлить", f"renew:{identity}"), ("Редактировать", f"fields:{identity}")],
                [("Скачать статистику", f"report:{server_id}"), ("Удалить", f"delete:{identity}")],
                [("🖥 Мои сервера", "servers:0"), ("🏠 Главное меню", "menu")]]
        await self.say(message, server_text(server, today=self.today()), keyboard(rows))

    async def list_statistics(self, message):
        servers = self.storage.statistics()
        active = [server for server in servers if not server["deleted_at"]]
        total = sum(server["total_paid_kopeks"] for server in servers)
        active_total = sum(server["total_paid_kopeks"] for server in active)
        unknown = sum(server["unknown_payments"] for server in servers)
        payments = [payment for server in servers for payment in server["payments"]]
        periods = sum(bool(payment["starts_on"] and payment["ends_on"]) for payment in payments)
        bonus = sum(payment["paid_with_bonus"] for payment in payments)
        rows = [[("📥 Скачать по всем серверам", "report:all")],
                [("🟢 Активные серверы", "stats:active:0"), ("🗃 Архивные серверы", "stats:archive:0")],
                [("🏠 Главное меню", "menu")]]
        text = ("📊 <b>Статистика серверов</b>\n\n"
                f"💰 <b>Расходы за всё время</b>\nСуммарно: <b>{format_money(total)}</b>\n"
                f"🟢 Активные серверы: {format_money(active_total)}\n"
                f"🗃 Архив: {format_money(total - active_total)}\n\n──────────────\n\n"
                f"🖥 <b>Серверы</b>\n🟢 Активных: <b>{len(active)}</b>\n🗃 В архиве: <b>{len(servers) - len(active)}</b>\n\n──────────────\n\n"
                f"🧾 <b>История оплат</b>\n📅 Периодов: <b>{periods}</b>\n")
        if len(payments) > periods:
            text += f"📝 Оплат без указанного периода: <b>{len(payments) - periods}</b>\n"
        text += (f"🎁 Бонусами оплачено: <b>{bonus}</b>\n"
                 f"❔ Периодов без суммы: <b>{unknown}</b>")
        if unknown:
            text += "\n⚠️ Итог неполный: учтены только известные суммы."
        text += "\n\n──────────────\n\n📥 <b>Выгрузка в Excel</b>\nВыберите группу серверов или скачайте общую историю оплат."
        await self.say(message, text, keyboard(rows))

    async def list_statistics_servers(self, message, group, page):
        servers = [server for server in self.storage.statistics()
                   if bool(server["deleted_at"]) == (group == "archive")]
        page = min(max(page, 0), max(0, (len(servers) - 1) // 10))
        buttons = [(server["name"][:30], f"report:{server['id']}")
                   for server in servers[page * 10:(page + 1) * 10]]
        rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        navigation = []
        if page:
            navigation.append(("← Назад", f"stats:{group}:{page - 1}"))
        if (page + 1) * 10 < len(servers):
            navigation.append(("Далее →", f"stats:{group}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.extend([[("← К статистике", "stats:0")], [("🏠 Главное меню", "menu")]])
        title = "Архивные серверы" if group == "archive" else "Активные серверы"
        text = (f"<b>{title} · страница {page + 1}</b>\n\n"
                + ("Выберите сервер, чтобы скачать историю оплат в Excel." if servers else "В этой группе пока нет серверов."))
        await self.say(message, text, keyboard(rows))

    async def send_statistics(self, message, server_id=None):
        servers = self.storage.statistics(server_id)
        if server_id is not None and not servers:
            await self.say(message, "Сервер не найден в статистике.", MENU)
            return
        data = await asyncio.to_thread(build_report, servers, single=server_id is not None,
                                       timezone=self.settings.timezone)
        name = f"server-{server_id}-statistics.xlsx" if server_id is not None else "servers-statistics.xlsx"
        await message.answer_document(BufferedInputFile(data, filename=name), caption="История оплаченных периодов и сумм в рублях.")
        panel_id = self.panels.pop(message.chat.id, None)
        self.panel_markups.pop(message.chat.id, None)
        if panel_id is not None:
            try:
                await message.bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=panel_id, reply_markup=None)
            except TelegramAPIError as error:
                logger.warning("Не снята клавиатура статистики: %s", type(error).__name__)
        await self.list_statistics(message)

    def form_keyboard(self, draft, confirm=False):
        rows = []
        if confirm:
            text = "Да, удалить" if draft.kind == "delete" else "Сохранить"
            rows.append([(text, f"save:{draft.token}")])
        rows.append([("Отмена", f"cancel:{draft.token}")])
        return keyboard(rows)

    async def prompt(self, message, draft):
        selected = draft.selected_field
        hint = " (ДД.ММ.ГГГГ)" if selected in ("starts_on", "ends_on") else ""
        if selected == "amount_kopeks":
            text = "Введите сумму оплаты в рублях, например 1500 или 1500,50."
        elif draft.kind == "renew":
            text = "Введите новую дату окончания оплаты (ДД.ММ.ГГГГ)."
        else:
            text = f"Введите: {LABELS[selected]}{hint}."
        draft.needs_prompt = True
        await self.say(message, text, self.form_keyboard(draft))
        draft.needs_prompt = False

    async def preview(self, message, draft):
        prefix = "Удалить этот сервер?" if draft.kind == "delete" else "Проверьте данные и нажмите «Сохранить»:"
        await self.say(message, f"{prefix}\n\n{server_text(draft.values, today=self.today())}",
                       self.form_keyboard(draft, confirm=True))

    async def stale(self, message, admin_id, server_id):
        self.drafts.pop(admin_id, None)
        await self.say(message, "Сервер уже изменён или удалён. Откройте актуальные данные.",
                       keyboard([[("Открыть сервер", f"view:{server_id}")], [("🏠 Главное меню", "menu")]]))

    async def on_message(self, message):
        await self._handle_message(message)
        try:
            await message.delete()
        except TelegramAPIError as error:
            logger.warning("Не удалён ввод администратора: %s", type(error).__name__)

    async def _handle_message(self, message):
        admin_id = message.from_user.id
        text = (message.text or "").strip()
        if text.split("@", 1)[0] in ("/start", "/cancel"):
            await self.menu(message, admin_id)
            return
        draft = self.drafts.get(admin_id)
        if draft is None:
            await self.say(message, "Выберите действие в меню.", MENU)
            return
        if draft.ready:
            await self.preview(message, draft)
            return
        if draft.needs_prompt:
            await self.prompt(message, draft)
            return
        selected = draft.selected_field
        try:
            value = validate_field(selected, text)
            updated = {**draft.values, selected: value}
            if draft.kind == "renew" and selected == "ends_on":
                if value <= draft.values["ends_on"]:
                    raise ValueError("Новая дата должна быть позже текущей даты окончания.")
                if value < self.today().isoformat():
                    raise ValueError("Новая дата не может быть раньше сегодняшней.")
                updated["payment_starts_on"] = (date.fromisoformat(draft.values["ends_on"]) + timedelta(days=1)).isoformat()
            if all(key in updated for key in FIELDS):
                updated.update(validate_server(updated, allow_empty_addresses=draft.kind != "add"))
                if draft.kind == "edit":
                    history = self.storage.payment_history(draft.server_id)
                    validate_payment_dates(updated, history)
                    if len(history) == 1:
                        updated["payment_starts_on"] = updated["starts_on"]
            if selected == "amount_kopeks":
                if draft.kind == "renew" or value != draft.values.get("amount_kopeks"):
                    updated["paid_with_bonus"] = 0
                if draft.kind == "add":
                    updated.update(total_paid_kopeks=value, known_payments=1, unknown_payments=0)
                elif draft.kind == "renew":
                    updated["total_paid_kopeks"] += value
                    updated["known_payments"] += 1
                else:
                    old_amount = draft.values["amount_kopeks"]
                    updated["total_paid_kopeks"] += value - (old_amount or 0)
                    updated["known_payments"] += int(old_amount is None)
                    updated["unknown_payments"] -= int(old_amount is None)
                validate_amount(updated["total_paid_kopeks"])
        except ValueError as error:
            await self.say(message, f"{error}\n\nПродолжим с текущего поля: {LABELS[selected]}.",
                           self.form_keyboard(draft))
            return
        draft.values = updated
        if draft.kind == "add" and selected != FORM_FIELDS[-1]:
            draft.selected_field = FORM_FIELDS[FORM_FIELDS.index(selected) + 1]
            await self.prompt(message, draft)
        elif draft.kind == "renew" and selected == "ends_on":
            draft.selected_field = "amount_kopeks"
            await self.prompt(message, draft)
        else:
            draft.ready = True
            await self.preview(message, draft)

    async def save(self, message, admin_id, draft):
        if draft.kind == "add":
            server = self.storage.add(draft.values)
            server_id = server["id"]
        else:
            server_id = draft.server_id
            saved = self.storage.get(server_id)
            if saved is None or saved["version"] != draft.version:
                await self.stale(message, admin_id, server_id)
                return
            if draft.kind == "delete":
                success = self.storage.delete(server_id, draft.version)
            else:
                if draft.kind == "renew" and draft.values["ends_on"] < self.today().isoformat():
                    draft.ready = False
                    draft.values = dict(saved)
                    draft.selected_field = "ends_on"
                    await self.say(message, "Дата уже в прошлом. Введите актуальную дату.", self.form_keyboard(draft))
                    return
                if draft.kind == "renew":
                    success = self.storage.renew(server_id, draft.version, draft.values["ends_on"], draft.values["amount_kopeks"])
                else:
                    success = self.storage.update(server_id, draft.version, draft.values)
            if not success:
                await self.stale(message, admin_id, server_id)
                return
        self.drafts.pop(admin_id, None)
        if draft.kind == "delete":
            await self.say(message, "Сервер удалён.", MENU)
        else:
            await self.card(message, server_id)

    async def on_callback(self, callback):
        await callback.answer()
        message, admin_id = callback.message, callback.from_user.id
        if (message.chat.id not in self.panels and message.text and message.reply_markup
                and any(button.callback_data in ("menu", "add")
                        or (button.callback_data or "").startswith(("cancel:", "save:", "stats:", "list:", "servers:", "view:"))
                        for row in message.reply_markup.inline_keyboard for button in row)):
            self.panels[message.chat.id] = message.message_id
            self.panel_markups[message.chat.id] = message.reply_markup
        parts = (callback.data or "").split(":")
        action = parts[0]
        if action == "menu" and len(parts) == 1:
            await self.menu(message, admin_id)
        elif action == "add" and len(parts) == 1:
            draft = Draft("add", selected_field=FIELDS[0])
            self.drafts[admin_id] = draft
            await self.prompt(message, draft)
        elif action in ("save", "cancel") and len(parts) == 2:
            draft = self.drafts.get(admin_id)
            if not draft or draft.token != parts[1]:
                await self.say(message, "Эта кнопка устарела. Используйте кнопки текущего действия.")
            elif action == "cancel":
                await self.menu(message, admin_id)
            elif draft.ready:
                await self.save(message, admin_id, draft)
        elif action == "report" and len(parts) == 2 and (parts[1] == "all" or parts[1].isdigit()):
            self.drafts.pop(admin_id, None)
            await self.send_statistics(message, None if parts[1] == "all" else int(parts[1]))
        elif action == "stats" and len(parts) == 3 and parts[1] in ("active", "archive") and parts[2].isdigit():
            self.drafts.pop(admin_id, None)
            await self.list_statistics_servers(message, parts[1], int(parts[2]))
        elif action in ("list", "servers", "view", "stats") and len(parts) == 2 and parts[1].isdigit():
            self.drafts.pop(admin_id, None)
            if action in ("list", "servers"):
                await self.list_servers(message, int(parts[1]), overview=action == "servers")
            elif action == "stats":
                await self.list_statistics(message)
            else:
                await self.card(message, int(parts[1]))
        elif (action in ("edit", "renew", "delete", "fields") and len(parts) == (4 if action == "edit" else 3)
              and parts[1].isdigit() and parts[2].isdigit()):
            server_id, version = int(parts[1]), int(parts[2])
            selected = parts[3] if action == "edit" else "ends_on"
            if selected not in FORM_FIELDS:
                return
            server = self.storage.get(server_id)
            if server is None or server["version"] != version:
                await self.stale(message, admin_id, server_id)
                return
            if action == "fields":
                self.drafts.pop(admin_id, None)
                rows = [[(label, f"edit:{server_id}:{version}:{key}")] for key, label in LABELS.items()]
                rows.append([("← К серверу", f"view:{server_id}")])
                await self.say(message, "✏️ Выберите данные для изменения:", keyboard(rows))
                return
            draft = Draft(action, values=dict(server),
                          server_id=server_id, version=version, selected_field=selected,
                          ready=action == "delete")
            self.drafts[admin_id] = draft
            if draft.ready:
                await self.preview(message, draft)
            else:
                await self.prompt(message, draft)


def create_dispatcher(storage, settings):
    ui = BotUI(storage, settings)
    router = Router()
    access = AdminOnly(settings.admin_ids)
    router.message.outer_middleware(access)
    router.callback_query.outer_middleware(access)
    router.message.register(ui.on_message)
    router.callback_query.register(ui.on_callback)

    @router.errors()
    async def on_error(event):
        logger.error("Ошибка обработки: %s", type(event.exception).__name__)
        if (isinstance(event.exception, TelegramNetworkError)
                and isinstance(event.exception.method, (SendMessage, EditMessageText))):
            return True
        message = event.update.message or (event.update.callback_query.message if event.update.callback_query else None)
        if isinstance(message, Message) and message.chat.type == "private" and message.chat.id in settings.admin_ids:
            try:
                await ui.say(message, "Не удалось выполнить действие. Попробуйте ещё раз или нажмите /cancel.")
            except TelegramAPIError as error:
                logger.warning("Не показана ошибка: %s", type(error).__name__)
        return True

    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher.include_router(router)
    return dispatcher
