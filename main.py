import argparse
import asyncio
import logging
import sqlite3
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError, TelegramUnauthorizedError

from bot import create_dispatcher
from config import Settings
from reminders import Notifier
from storage import Storage


logger = logging.getLogger(__name__)


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secrets = sorted({secret for secret in secrets if secret}, key=len, reverse=True)

    def format(self, record):
        rendered = super().format(record)
        for secret in self.secrets:
            rendered = rendered.replace(secret, "[скрыто]")
        return rendered


def configure_logging(settings):
    secrets = [settings.token, quote(settings.token, safe=""), settings.proxy_url]
    if settings.proxy_url:
        parsed = urlsplit(settings.proxy_url)
        secrets.extend([parsed.username, parsed.password,
                        unquote(parsed.username or ""), unquote(parsed.password or "")])
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(secrets))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


async def run(settings, check=False):
    session = AiohttpSession(proxy=settings.proxy_url, timeout=20)
    bot = Bot(settings.token, session=session)
    storage = None
    dispatcher = None
    tasks = []
    try:
        me = await bot.get_me()
        connection = "через прокси" if settings.proxy_url else "напрямую"
        logger.info("Telegram доступен: @%s, подключение %s.", me.username, connection)
        if check:
            logger.info("Конфигурация и подключение проверены. Рассылка не запускалась.")
            return
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise ValueError("Для этого токена включён webhook. Отключите его перед запуском polling.")
        storage = Storage(settings.database_path)
        dispatcher = create_dispatcher(storage, settings)
        notifier = Notifier(bot, storage, settings)
        logger.info("Рассылка: %s, %s, за %s дн.; просрочка: %s.", settings.notify_time.strftime("%H:%M"),
                    settings.timezone.key, settings.remind_days, settings.notify_overdue)
        tasks = [asyncio.create_task(dispatcher.start_polling(
            bot, close_bot_session=False, allowed_updates=["message", "callback_query"])),
                 asyncio.create_task(notifier.run())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if dispatcher:
            await dispatcher.fsm.close()
        if storage:
            storage.close()
        await session.close()


def cli():
    parser = argparse.ArgumentParser(description="Напоминания об оплате серверов")
    parser.add_argument("--check", action="store_true", help="проверить настройки и Telegram без запуска бота")
    args = parser.parse_args()
    try:
        settings = Settings.load(Path(__file__).resolve().parent)
        configure_logging(settings)
        asyncio.run(run(settings, check=args.check))
    except ValueError as error:
        # Текст ошибок конфигурации не содержит значения токена или прокси.
        print(f"Ошибка: {error}")
        return 1
    except TelegramUnauthorizedError:
        logger.error("Telegram отклонил токен. Проверьте BOT_TOKEN в .env.")
        return 1
    except TelegramConflictError:
        logger.error("Этот токен уже используется другим процессом. Остановите второй экземпляр.")
        return 1
    except (TelegramNetworkError, OSError):
        logger.error("Не удалось подключиться. Проверьте интернет и PROXY_URL в .env.")
        return 1
    except sqlite3.Error:
        logger.error("Не удалось открыть или изменить SQLite. Проверьте путь и права на каталог базы.")
        return 1
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
