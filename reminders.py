import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from html import escape

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from storage import format_money


logger = logging.getLogger(__name__)


def display_date(value):
    return date.fromisoformat(value).strftime("%d.%m.%Y")


def server_text(server, *, today=None):
    today = today or date.today()
    remaining = (date.fromisoformat(server["ends_on"]) - today).days
    status = f"🟢 Оплачен · осталось {remaining} дн." if remaining > 0 else (
        "🟡 Оплата заканчивается сегодня" if remaining == 0 else f"🔴 Просрочен на {-remaining} дн.")
    amount = server.get("amount_kopeks")
    bonus = " · Оплачено бонусами" if server.get("paid_with_bonus") else ""
    known = server.get("known_payments", int(amount is not None))
    unknown = server.get("unknown_payments", int(amount is None))
    total = format_money(server.get("total_paid_kopeks", amount or 0)) if known else "не указано"
    if unknown and known:
        total += f" (итог неполный; без суммы: {unknown})"
    starts_on = server.get("payment_starts_on", server["starts_on"])
    rental = f"\nАренда с {display_date(server['starts_on'])}" if starts_on != server["starts_on"] else ""
    ip = f"<code>{escape(server['ip_address'])}</code>" if server["ip_address"] else "не указан"
    domain = f"<code>{escape(server['domain'])}</code>" if server["domain"] else "не указан"
    return (f"<b>{escape(server['name'])}</b>\n"
            f"🏢 <a href=\"{escape(server['website'], quote=True)}\">{escape(server['hoster'])} · кабинет и оплата</a>\n\n"
            f"<b>Подключение</b>\nIP-адрес: {ip}\nДомен: {domain}\n\n"
            f"<b>Последний оплаченный период</b>\n"
            f"📅 {display_date(starts_on)} — {display_date(server['ends_on'])}\n{status}{rental}\n\n"
            f"<b>Расходы</b>\nЗа период: {format_money(amount)}{bonus}\nВсего оплачено: {total}")


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
                        admin_id, f"{status}\n\n{server_text(server, today=today)}", parse_mode="HTML",
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
