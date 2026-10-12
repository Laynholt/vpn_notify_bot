from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, GetWebhookInfo, SendDocument, SendMessage
from aiogram.types import Chat, Message, User, WebhookInfo


class TelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.failures = {}
        self.document_failures = {}

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, GetMe):
            return User(id=bot.id, is_bot=True, first_name="Bot", username="test_bot")
        if isinstance(method, GetWebhookInfo):
            return WebhookInfo(url="", has_custom_certificate=False, pending_update_count=0)
        if isinstance(method, SendMessage):
            if method.chat_id in self.failures:
                raise self.failures[method.chat_id](method)
            return Message(message_id=len(self.requests), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type="private"), text=method.text,
                           reply_markup=method.reply_markup)
        if isinstance(method, SendDocument):
            if method.chat_id in self.document_failures:
                raise self.document_failures[method.chat_id](method)
            return Message(message_id=len(self.requests), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type="private"))
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536,
                             raise_for_status=True):
        yield b""

    @property
    def messages(self):
        return [method for method in self.requests if isinstance(method, SendMessage)]

    @property
    def documents(self):
        return [method for method in self.requests if isinstance(method, SendDocument)]


def telegram_bot():
    session = TelegramSession()
    return Bot("123456789:abcdefghijklmnopqrstuvwxyz012345678", session=session), session
