import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiogram.methods import GetMe

from config import Settings
from helpers import TelegramSession
from main import RedactingFormatter, run


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_connects_through_configured_proxy_without_creating_database(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / ".env").write_text("BOT_TOKEN=123:abc\nADMIN_IDS=123\n"
                                       "PROXY_URL=http://127.0.0.1:10808", encoding="utf-8")
            settings = Settings.load(base)
            session = TelegramSession()
            with patch("main.AiohttpSession", return_value=session) as factory:
                await run(settings, check=True)
            self.assertEqual(factory.call_args.kwargs["proxy"], "http://127.0.0.1:10808")
            self.assertEqual(len(session.requests), 1)
            self.assertIsInstance(session.requests[0], GetMe)
            self.assertFalse(settings.database_path.exists())

    async def test_empty_proxy_creates_direct_session(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / ".env").write_text("BOT_TOKEN=123:abc\nADMIN_IDS=123", encoding="utf-8")
            with patch("main.AiohttpSession", return_value=TelegramSession()) as factory:
                await run(Settings.load(base), check=True)
            self.assertIsNone(factory.call_args.kwargs["proxy"])

    async def test_logs_redact_token_and_proxy_credentials_even_in_traceback(self):
        formatter = RedactingFormatter(["123:abc", "user", "password"])
        try:
            raise ValueError("url /bot123:abc / proxy user:password")
        except ValueError:
            import sys
            record = logging.LogRecord("test", logging.ERROR, "test", 1, "token %s",
                                       ("123:abc",), sys.exc_info())
        rendered = formatter.format(record)
        for secret in ("123:abc", "user", "password"):
            self.assertNotIn(secret, rendered)


if __name__ == "__main__":
    unittest.main()
