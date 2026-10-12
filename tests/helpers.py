from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import DeleteMessage, EditMessageReplyMarkup, EditMessageText, GetMe, GetWebhookInfo, SendDocument, SendMessage
from aiogram.types import Chat, Document, Message, User, WebhookInfo


class TelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.failures = {}
        self.document_failures = {}
        self.edit_failures = {}
        self.delete_failures = {}
        self.sent_messages = {}

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
            message = Message(message_id=1000 + len(self.requests), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type="private"), text=method.text,
                           from_user=User(id=bot.id, is_bot=True, first_name="Bot"),
                           reply_markup=method.reply_markup)
            self.sent_messages[(method.chat_id, message.message_id)] = message
            return message
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup)):
            if method.chat_id in self.edit_failures:
                raise self.edit_failures[method.chat_id](method)
            saved = self.sent_messages.get((method.chat_id, method.message_id))
            message = Message(message_id=method.message_id, date=datetime.now(timezone.utc),
                              chat=Chat(id=method.chat_id, type="private"),
                              from_user=User(id=bot.id, is_bot=True, first_name="Bot"),
                              text=method.text if isinstance(method, EditMessageText) else saved.text if saved else None,
                              reply_markup=method.reply_markup)
            self.sent_messages[(method.chat_id, message.message_id)] = message
            return message
        if isinstance(method, DeleteMessage):
            if method.chat_id in self.delete_failures:
                raise self.delete_failures[method.chat_id](method)
            self.sent_messages.pop((method.chat_id, method.message_id), None)
            return True
        if isinstance(method, SendDocument):
            if method.chat_id in self.document_failures:
                raise self.document_failures[method.chat_id](method)
            message = Message(message_id=1000 + len(self.requests), date=datetime.now(timezone.utc),
                              chat=Chat(id=method.chat_id, type="private"), caption=method.caption,
                              from_user=User(id=bot.id, is_bot=True, first_name="Bot"),
                              document=Document(file_id="file", file_unique_id="unique"))
            self.sent_messages[(method.chat_id, message.message_id)] = message
            return message
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536,
                             raise_for_status=True):
        yield b""

    @property
    def messages(self):
        return [method for method in self.requests if isinstance(method, (SendMessage, EditMessageText))]

    @property
    def documents(self):
        return [method for method in self.requests if isinstance(method, SendDocument)]


def telegram_bot():
    session = TelegramSession()
    return Bot("123456789:abcdefghijklmnopqrstuvwxyz012345678", session=session), session
