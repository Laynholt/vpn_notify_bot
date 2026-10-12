import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from html import escape

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions


logger = logging.getLogger(__name__)


def display_date(value):
    return date.fromisoformat(value).strftime("%d.%m.%Y")


def server_text(server):
    return (f"<b>{escape(server['name'])}</b>\n"
            f"Хостер: {escape(server['hoster'])}\n"
            f"Сайт: {escape(server['website'])}\n"
            f"IP-адрес: {escape(server['ip_address'] or 'не указан')}\n"
            f"Домен: {escape(server['domain'] or 'не указан')}\n"
            f"Оплачен: {display_date(server['starts_on'])} — {display_date(server['ends_on'])}")


def reminder_keyboard(server, today):
    identity = f"{server['id']}:{server['version']}"
    rows = [[InlineKeyboardButton(text="Продлил", callback_data=f"renew:{identity}")]]
    rows.append([InlineKeyboardButton(text="🌐 Открыть хостер", url=server["website"])])
    if date.fromisoformat(server["ends_on"]) < today:
        rows.append([InlineKeyboardButton(text="Удалить", callback_data=f"delete:{identity}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class Notifier:
    def __init__(self, bot, storage, settings):
        self.bot, self.storage, self.settings = bot, storage, settings
        self.retry_at = {}

    async def tick(self, now):
        local = now.astimezone(self.settings.timezone)
        if local.time().replace(tzinfo=None) < self.settings.notify_time:
            return
        started = time.monotonic()
        today = local.date()
        for snapshot in self.storage.list():
            for admin_id in self.settings.admin_ids:
                if now < self.retry_at.get(admin_id, now):
                    continue
                server = self.storage.get(snapshot["id"])
                if server is None:
                    break
                remaining = (date.fromisoformat(server["ends_on"]) - today).days
                if remaining > self.settings.remind_days or (remaining < 0 and not self.settings.notify_overdue):
                    continue
                day = today.isoformat()
                if self.storage.delivered(server["id"], server["ends_on"], admin_id, day):
                    continue
                if remaining < 0:
                    status = f"⚠️ Просрочен на {abs(remaining)} дн. Продлите или удалите сервер."
                elif remaining == 0:
                    status = "⚠️ Оплаченный период заканчивается сегодня. Не забудьте продлить."
                else:
                    status = f"🔔 До окончания оплаты: {remaining} дн. Не забудьте продлить."
                try:
                    await self.bot.send_message(
                        admin_id, f"{status}\n\n{server_text(server)}", parse_mode="HTML",
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                        reply_markup=reminder_keyboard(server, today))
                except TelegramAPIError as error:
                    delay = error.retry_after if isinstance(error, TelegramRetryAfter) else 60
                    if isinstance(error, TelegramForbiddenError):
                        delay = 3600
                    self.retry_at[admin_id] = now + timedelta(seconds=delay + time.monotonic() - started)
                    logger.warning("Не доставлено: сервер %s, администратор %s, %s",
                                   server["id"], admin_id, type(error).__name__)
                else:
                    self.storage.mark_delivered(server["id"], server["ends_on"], admin_id, day)
                await asyncio.sleep(1.05)

    async def run(self):
        while True:
            await self.tick(datetime.now(timezone.utc))
            await asyncio.sleep(30)
