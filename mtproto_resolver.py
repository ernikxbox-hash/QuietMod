"""🛰 MTProto-слой (опция): резолв юзернеймов + реальная статистика чатов.

Bot API не умеет двух вещей, которые умеет нативный Telegram-протокол:
1. Искать юзеров по @username (getChat по username — только каналы/группы).
2. Считать сообщения в чате и видеть дату создания — для .status нужны
   реальные цифры Telegram, а не то, что успел собрать архив.

Включение: env TELEGRAM_API_ID и TELEGRAM_API_HASH (my.telegram.org →
API development tools). Источник сессии:
- TELEGRAM_USER_SESSION (рекомендуется для .status): StringSession
  владельца — полный доступ ко всем его чатам, включая личные бизнес-
  чаты. Получить строку: session-string от Telethon (qr-логин или
  телефон).
- иначе — бот-токен: работает для резолва юзернеймов и для групп/
  каналов, где бот участник; личные чаты недоступны.

Любая ошибка MTProto никогда не роняет бота: функции возвращают None,
дальше идёт запасной путь (база / архив).
"""
import asyncio
import os
from typing import Optional

from core import BOT_TOKEN, log

_API_ID = os.environ.get("TELEGRAM_API_ID", "").strip()
_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
# StringSession владельца — полный доступ к его чатам (для .status).
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


# ── Реальная статистика чата для .status ──────────────────────────
# Bot API не умеет считать сообщения в чате — только MTProto. Кэш 60 сек,
# чтобы частые .status не дёргали API на каждый вызов.
_chat_stats_cache: dict[int, tuple[float, dict]] = {}
_CHAT_STATS_TTL = 60.0
_CHAT_STATS_MAX = 200


async def get_chat_stats_mtproto(peer_id: int) -> Optional[dict]:
    """Реальная статистика чата через MTProto: (id, дата создания, счётчики).

    Возвращает None, если MTProto не настроен или чат недоступен
    (для user-сессии — любой личный чат владельца; для бот-токена —
    только группы/каналы, где бот участник).

    Ключи: peer_id, created (datetime|None — дата создания чата),
    first_date (datetime|None — первое сообщение), total, photos, video,
    voice, video_note, gif, music, documents.
    """
    if not mtproto_enabled():
        return None
    now = asyncio.get_running_loop().time()
    cached = _chat_stats_cache.get(peer_id)
    if cached and now - cached[0] < _CHAT_STATS_TTL:
        return cached[1]
    try:
        client = await _get_client()
        if client is None:
            return None
        ent = await client.get_entity(peer_id)
        # счётчики: total — все сообщения; фильтры — по типам медиа
        from telethon.tl.patched import (
            InputMessagesFilterDocument,
            InputMessagesFilterGif,
            InputMessagesFilterMusic,
            InputMessagesFilterPhotos,
            InputMessagesFilterRoundVideo,
            InputMessagesFilterVideo,
            InputMessagesFilterVoice,
        )

        async def _count(flt=None) -> int:
            # limit=0 — единственный способ получить .total в Telethon:
            # он возвращает пустой список с count из ответа API. При limit=1
            # .total не заполняется и все счётчики молча стали бы 0.
            try:
                msgs = await client.get_messages(ent, limit=0, search_filter=flt)
                return getattr(msgs, "total", 0) or 0
            except Exception:
                return 0

        # первое сообщение (дата старта переписки)
        first = None
        try:
            msgs = await client.get_messages(ent, limit=1, reverse=True)
            if msgs:
                first = msgs[0].date
        except Exception:
            pass
        stats = {
            "peer_id": peer_id,
            "created": getattr(ent, "date", None),
            "first_date": first,
            "total": await _count(),
            "photos": await _count(InputMessagesFilterPhotos()),
            "video": await _count(InputMessagesFilterVideo()),
            "voice": await _count(InputMessagesFilterVoice()),
            "video_note": await _count(InputMessagesFilterRoundVideo()),
            "gif": await _count(InputMessagesFilterGif()),
            "music": await _count(InputMessagesFilterMusic()),
            "documents": await _count(InputMessagesFilterDocument()),
        }
        if len(_chat_stats_cache) >= _CHAT_STATS_MAX:
            _chat_stats_cache.clear()
        _chat_stats_cache[peer_id] = (now, stats)
        return stats
    except Exception as e:
        log.warning(f"🛰 mtproto chat stats peer={peer_id}: {e}")
        return None


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
