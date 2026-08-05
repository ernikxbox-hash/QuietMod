"""🛰 MTProto-резолвер юзернеймов (опция): .sled / .info находят людей по @username.

Bot API не умеет искать юзеров по юзернейму: getChat по username работает
только для каналов и супергрупп. Нативный Telegram-протокол (MTProto)
умеет — Telethon с бот-токеном резолвит любой публичный @username
в числовой ID (дальше по ID работают getChat и getUserProfilePhotos).

Включение: задай env-переменные TELEGRAM_API_ID и TELEGRAM_API_HASH
(берутся на my.telegram.org → API development tools, занимает минуту).
Без них модуль молча не работает — бот фолбэкает на свою базу
(люди, с кем уже был чат) и на числовые ID. Любая ошибка MTProto тоже
никогда не роняет бота: резолвер возвращает None, дальше идёт запасной
путь с понятным сообщением пользователю.
"""
import asyncio
import os
from typing import Optional

from core import BOT_TOKEN, log

_API_ID = os.environ.get("TELEGRAM_API_ID", "").strip()
_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()

_client = None                       # TelegramClient — синглтон на процесс
_client_lock: Optional[asyncio.Lock] = None


def mtproto_enabled() -> bool:
    """Резолвер настроен (api_id + api_hash заданы в env)."""
    return bool(_API_ID and _API_HASH)


async def _get_client():
    """Лениво создаёт и логинит MTProto-клиент (бот-токеном). None — не вышло."""
    global _client, _client_lock
    if not mtproto_enabled():
        return None
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    async with _client_lock:
        if _client is not None and _client.is_connected():
            return _client
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            client = TelegramClient(StringSession(), int(_API_ID), _API_HASH)
            await client.start(bot_token=BOT_TOKEN)
            _client = client
            log.info("🛰 MTProto-клиент подключён (резолвер юзернеймов готов)")
        except Exception as e:
            log.warning(f"🛰 MTProto client init: {e}")
            _client = None
    return _client


async def resolve_username_mtproto(username: str) -> Optional[dict]:
    """@username → {id, username, full_name, bio} через MTProto. None — не вышло.

    Возвращает базовые данные юзера; bio из резолва для не-контактов
    недоступен, поэтому всегда пустой — полный профиль берётся getChat по ID.
    """
    if not mtproto_enabled():
        return None
    s = (username or "").lstrip("@").strip()
    if not s:
        return None
    try:
        client = await _get_client()
        if client is None:
            return None
        ent = await client.get_entity(s)
        eid = getattr(ent, "id", None)
        if not eid:
            return None
        first = getattr(ent, "first_name", "") or ""
        last = getattr(ent, "last_name", "") or ""
        full_name = (f"{first} {last}".strip()) or getattr(ent, "title", "") or ""
        return {
            "id": int(eid),
            "username": (getattr(ent, "username", "") or "").lstrip("@"),
            "full_name": full_name,
            "bio": "",
        }
    except Exception as e:
        log.warning(f"🛰 mtproto resolve '@{s}': {e}")
        return None
