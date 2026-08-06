"""🛰 MTProto-слой (опция): резолв юзернеймов для .sled/.info.

Bot API умеет находить по @username только каналы и супергруппы — людей
так искать нельзя. MTProto (Telethon) умеет резолвить ЛЮБОГО публичного
юзера: `@username` → id/full_name в .sled, .info и других командах.

Включение: env TELEGRAM_API_ID и TELEGRAM_API_HASH (my.telegram.org →
API development tools). Источник сессии:
- TELEGRAM_USER_SESSION (рекомендуется): StringSession владельца — резолв
  любых публичных юзеров, даже тех, кого бот никогда не видел. Получить
  строку: session-string от Telethon (qr-логин или телефон).
- иначе — бот-токен: работает для резолва и для групп/каналов, где бот
  участник; личные чаты и чужие юзеры недоступны.

Любая ошибка MTProto никогда не роняет бота: функции возвращают None,
дальше идёт запасной путь (база / архив).
"""
import asyncio
import os
from typing import Optional

from core import BOT_TOKEN, log

_API_ID = os.environ.get("TELEGRAM_API_ID", "").strip()
_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
# StringSession владельца — полный доступ к его аккаунту: резолв любых
# публичных юзеров по @username (для .sled/.info), даже если бот их
# никогда не видел. С бот-токеном MTProto видит только то, что видит бот.
_USER_SESSION = os.environ.get("TELEGRAM_USER_SESSION", "").strip()

_client = None                       # TelegramClient — синглтон на процесс
_client_lock: Optional[asyncio.Lock] = None


def mtproto_enabled() -> bool:
    """MTProto настроен (api_id + api_hash заданы в env)."""
    return bool(_API_ID and _API_HASH)


async def _get_client():
    """Лениво создаёт и логинит MTProto-клиент. None — не вышло.

    Приоритет сессии: TELEGRAM_USER_SESSION (владелец) → бот-токен.
    """
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
            if _USER_SESSION:
                client = TelegramClient(StringSession(_USER_SESSION), int(_API_ID), _API_HASH)
                await client.connect()
                await client.get_me()  # проверим, что сессия жива
                log.info("🛰 MTProto-клиент подключён (user-сессия владельца)")
            else:
                client = TelegramClient(StringSession(), int(_API_ID), _API_HASH)
                await client.start(bot_token=BOT_TOKEN)
                log.info("🛰 MTProto-клиент подключён (бот-токен)")
            _client = client
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
