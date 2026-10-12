import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    admin_ids: tuple[int, ...]
    notify_time: time
    timezone: ZoneInfo
    remind_days: int
    notify_overdue: bool
    database_path: Path
    proxy_url: str | None = field(default=None, repr=False)

    @classmethod
    def load(cls, base: Path):
        values = dotenv_values(base / ".env", encoding="utf-8-sig", interpolate=False)

        def value(key, default=""):
            return (values.get(key) or default).strip()

        token = value("BOT_TOKEN")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("Заполните BOT_TOKEN токеном от @BotFather в .env.")
        ids = value("ADMIN_IDS").split(",")
        if any(not re.fullmatch(r"[1-9][0-9]*", item.strip()) for item in ids):
            raise ValueError("ADMIN_IDS: нужны положительные Telegram ID через запятую.")
        clock = value("NOTIFY_TIME", "12:00")
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock):
            raise ValueError("NOTIFY_TIME: используйте HH:MM, например 12:00.")
        try:
            timezone = ZoneInfo(value("TIMEZONE", "Europe/Moscow"))
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("TIMEZONE: неизвестный часовой пояс IANA.") from None
        days = value("REMIND_DAYS", "7")
        if not re.fullmatch(r"[0-9]+", days):
            raise ValueError("REMIND_DAYS: нужно целое число от нуля.")
        overdue = value("NOTIFY_OVERDUE", "true").lower()
        if overdue not in ("true", "false"):
            raise ValueError("NOTIFY_OVERDUE: используйте true или false.")
        proxy = value("PROXY_URL") or None
        if proxy:
            try:
                parsed = urlsplit(proxy)
                valid = (parsed.scheme in ("http", "socks5") and parsed.hostname
                         and parsed.port and not parsed.query and not parsed.fragment
                         and parsed.path in ("", "/")
                         and not any(char.isspace() for char in proxy))
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("PROXY_URL: нужен http://host:port или socks5://host:port.")
        return cls(token, tuple(dict.fromkeys(int(item) for item in ids)),
                   time.fromisoformat(clock), timezone, int(days), overdue == "true",
                   (base / value("DATABASE_PATH", "data/servers.sqlite3")).resolve(), proxy)
