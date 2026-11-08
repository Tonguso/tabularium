"""Reject updates before Telegram dispatch; ownership never follows active_chat.json."""
import os
from pathlib import Path
import time
import telebot
from agent_runtime import atomic_json

OWNER_USER_ID = int(os.environ.get("OWNER_TELEGRAM_USER_ID", "123456789"))   # your Telegram user id
OWNER_CHAT_ID = int(os.environ.get("OWNER_TELEGRAM_CHAT_ID", str(OWNER_USER_ID)))


def is_owner(message):
    return (getattr(getattr(message, 'from_user', None), 'id', None) == OWNER_USER_ID
            and getattr(getattr(message, 'chat', None), 'id', None) == OWNER_CHAT_ID
            and getattr(message.chat, 'type', None) == 'private')


class OwnerBot(telebot.TeleBot):
    def __init__(self, token, directory):
        super().__init__(token)
        self.transport_file = Path(directory) / 'task_state' / 'transport.json'

    def get_updates(self, *args, **kwargs):
        try:
            updates = super().get_updates(*args, **kwargs)
        except Exception as exc:
            # Do not write exception strings: Telegram URLs can contain the bot token.
            atomic_json(self.transport_file, {'pid': os.getpid(), 'time': time.time(),
                        'poll_ok': False, 'error': type(exc).__name__})
            raise
        atomic_json(self.transport_file, {'pid': os.getpid(), 'time': time.time(), 'poll_ok': True})
        return updates

    def process_new_updates(self, updates):
        # Advance the offset even for rejected updates, so they aren't fetched forever.
        if updates:
            self.last_update_id = max(self.last_update_id, max(u.update_id for u in updates))
        allowed = [u for u in updates if u.message is not None and is_owner(u.message)]
        return super().process_new_updates(allowed)
