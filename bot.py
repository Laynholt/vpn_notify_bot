import logging
from dataclasses import dataclass, field
from datetime import datetime
from secrets import token_hex

from aiogram import BaseMiddleware, Dispatcher, Router
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from reminders import server_text
from storage import FIELDS, validate_field, validate_server


LABELS = {"name": "Название сервера", "hoster": "Название хостера", "website": "Сайт хостера",
          "starts_on": "Дата начала аренды", "ends_on": "Дата окончания оплаты"}
logger = logging.getLogger(__name__)


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows])


MENU = keyboard([[('Добавить сервер', 'add')], [('Изменить сервер', 'list:0')]])


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


class BotUI:
    def __init__(self, storage, settings):
        self.storage, self.settings = storage, settings
        self.drafts = {}

    def today(self):
        return datetime.now(self.settings.timezone).date()

    async def say(self, message, text, markup=None):
        await message.answer(text, reply_markup=markup, parse_mode="HTML",
                             link_preview_options=LinkPreviewOptions(is_disabled=True))

    async def menu(self, message, admin_id):
        self.drafts.pop(admin_id, None)
        await self.say(message, "Управление оплатой серверов", MENU)

    async def list_servers(self, message, page):
        servers = self.storage.list()
        if not servers:
            await self.say(message, "Серверов пока нет.", MENU)
            return
        page = min(max(page, 0), (len(servers) - 1) // 10)
        rows = [[(item["name"][:60], f"view:{item['id']}")]
                for item in servers[page * 10:(page + 1) * 10]]
        navigation = []
        if page:
            navigation.append(("← Назад", f"list:{page - 1}"))
        if (page + 1) * 10 < len(servers):
            navigation.append(("Далее →", f"list:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([("Главное меню", "menu")])
        await self.say(message, f"Выберите сервер (страница {page + 1}):", keyboard(rows))

    async def card(self, message, server_id):
        server = self.storage.get(server_id)
        if server is None:
            await self.say(message, "Сервер уже удалён.", MENU)
            return
        identity = f"{server['id']}:{server['version']}"
        rows = [[(label, f"edit:{identity}:{key}")] for key, label in LABELS.items()]
        rows.extend([[("Продлил", f"renew:{identity}"), ("Удалить", f"delete:{identity}")],
                     [("К списку", "list:0"), ("Главное меню", "menu")]])
        await self.say(message, server_text(server), keyboard(rows))

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
        if draft.kind == "renew":
            text = "Введите новую дату окончания оплаты (ДД.ММ.ГГГГ)."
        else:
            text = f"Введите: {LABELS[selected]}{hint}."
        await self.say(message, text, self.form_keyboard(draft))

    async def preview(self, message, draft):
        prefix = "Удалить этот сервер?" if draft.kind == "delete" else "Проверьте данные и нажмите «Сохранить»:"
        await self.say(message, f"{prefix}\n\n{server_text(draft.values)}",
                       self.form_keyboard(draft, confirm=True))

    async def stale(self, message, admin_id, server_id):
        self.drafts.pop(admin_id, None)
        await self.say(message, "Сервер уже изменён или удалён. Откройте актуальные данные.",
                       keyboard([[("Открыть сервер", f"view:{server_id}")], [("Главное меню", "menu")]]))

    async def on_message(self, message):
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
        selected = draft.selected_field
        try:
            value = validate_field(selected, text)
            updated = {**draft.values, selected: value}
            if draft.kind == "renew":
                if value <= draft.values["ends_on"]:
                    raise ValueError("Новая дата должна быть позже текущей даты окончания.")
                if value < self.today().isoformat():
                    raise ValueError("Новая дата не может быть раньше сегодняшней.")
            if all(key in updated for key in FIELDS):
                updated = validate_server(updated)
        except ValueError as error:
            await self.say(message, str(error), self.form_keyboard(draft))
            return
        draft.values = updated
        if draft.kind == "add" and selected != FIELDS[-1]:
            draft.selected_field = FIELDS[FIELDS.index(selected) + 1]
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
                    await self.say(message, "Дата уже в прошлом. Введите актуальную дату.", self.form_keyboard(draft))
                    return
                success = self.storage.update(server_id, draft.version, draft.values)
            if not success:
                await self.stale(message, admin_id, server_id)
                return
        self.drafts.pop(admin_id, None)
        if draft.kind == "delete":
            await self.say(message, "Сервер удалён.", MENU)
        else:
            await self.say(message, "Сохранено.")
            await self.card(message, server_id)

    async def on_callback(self, callback):
        await callback.answer()
        message, admin_id = callback.message, callback.from_user.id
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
        elif action in ("list", "view") and len(parts) == 2 and parts[1].isdigit():
            self.drafts.pop(admin_id, None)
            if action == "list":
                await self.list_servers(message, int(parts[1]))
            else:
                await self.card(message, int(parts[1]))
        elif (action in ("edit", "renew", "delete") and len(parts) == (4 if action == "edit" else 3)
              and parts[1].isdigit() and parts[2].isdigit()):
            server_id, version = int(parts[1]), int(parts[2])
            selected = parts[3] if action == "edit" else "ends_on"
            if selected not in FIELDS:
                return
            server = self.storage.get(server_id)
            if server is None or server["version"] != version:
                await self.stale(message, admin_id, server_id)
                return
            draft = Draft(action, values={key: server[key] for key in FIELDS},
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
        message = event.update.message or (event.update.callback_query.message if event.update.callback_query else None)
        if isinstance(message, Message) and message.chat.type == "private" and message.chat.id in settings.admin_ids:
            await ui.say(message, "Не удалось выполнить действие. Попробуйте ещё раз или нажмите /cancel.")
        return True

    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher.include_router(router)
    return dispatcher
