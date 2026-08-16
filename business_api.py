import asyncio
from typing import Optional

import database as db
from core import BOT_TOKEN, HTTP_SHORT_TIMEOUT, bot, get_http, log

# ── Владелец business-connection (кэш 10 мин) ─────────────────────────
_BC_OWNER_CACHE: dict[str, tuple[int, float]] = {}
_BC_OWNER_TTL_SECONDS = 10 * 60

async def _get_owner_id_cached(conn_id: str, ctx: str) -> Optional[int]:
    """ID владельца подключения — без него не определить хозяина бизнес-чата."""
    now = asyncio.get_running_loop().time()
    cached = _BC_OWNER_CACHE.get(conn_id)
    if cached and cached[1] > now:
        return cached[0]
    try:
        conn = await bot.get_business_connection(conn_id)
        owner_id = conn.user.id
    except Exception as e:
        log.error(f"get_business_connection ({ctx}): {e}")
        return None
    _BC_OWNER_CACHE[conn_id] = (owner_id, now + _BC_OWNER_TTL_SECONDS)
    # Дополнительно фиксируем владельца в БД: апдейт business_connection приходит
    # только при подключении/изменении, а этот кэш-промах случается на первом же
    # бизнес-сообщении — так в админке видны и подключения, сделанные ДО деплоя.
    try:
        await db.upsert_business_owner(
            owner_id,
            (conn.user.username or "") if conn.user else "",
            (conn.user.full_name or "") if conn.user else "",
            conn_id,
        )
        await db.upsert_user(
            owner_id,
            (conn.user.username or "") if conn.user else "",
            (conn.user.full_name or "") if conn.user else "",
        )
    except Exception as e:
        log.warning(f"business owner upsert ({ctx}): {e}")
    return owner_id

# ── Правки, сделанные самим ботом ─────────────────────────────────────
_BOT_EDITED_TTL_SECONDS = 120  # правка и её апдейт идут друг за другом — секунд хватает
_BOT_EDITED: dict[tuple[int, int], float] = {}  # (chat_id, msg_id) -> время правки


def _mark_bot_edited(chat_id: int, msg_id: int) -> None:
    """Запоминаем, что сообщение отредактировал сам бот (через Business API).

    Telegram присылает апдейт edited_business_message и на правки, которые
    сделал бот (форматирование .bold, статусы «◆ · · ·», результаты .curs
    и т.п.). Перехватчик должен такие правки пропускать — иначе бот сам
    себе шлёт «✦ Сообщение отредактировано» при обработке собственных
    команд. Запись живёт не дольше TTL: правка и её апдейт приходят
    практически одновременно.
    """
    now = asyncio.get_running_loop().time()
    if len(_BOT_EDITED) > 1000:  # ленивая чистка старых записей
        stale = [k for k, t in _BOT_EDITED.items() if now - t > _BOT_EDITED_TTL_SECONDS]
        for k in stale:
            _BOT_EDITED.pop(k, None)
    _BOT_EDITED[(chat_id, msg_id)] = now


def _is_bot_edited(chat_id: int, msg_id: int) -> bool:
    """Эту правку сделал сам бот? (проверка со снятием отметки — одноразовая)"""
    return _BOT_EDITED.pop((chat_id, msg_id), None) is not None


# ── Business-методы Telegram Bot API (через общую aiohttp-сессию) ─────
async def _business_edit_message_ex(conn_id: str, chat_id: int, msg_id: int, text: str, reply_markup=None) -> tuple[bool, str | None]:
    # Правку делает сам бот — перехватчик должен пропустить её апдейт.
    # Ставим отметку ЗДЕСЬ (в самой низкоуровневой функции), чтобы была
    # покрыта ЛЮБАЯ бизнес-правка бота — через _business_edit_message,
    # _business_edit_ai_html и любые будущие обёртки.
    _mark_bot_edited(chat_id, msg_id)
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "business_connection_id": conn_id,
        "chat_id": chat_id,
        "message_id": msg_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        session = get_http()
        async with session.post(url, json=payload, timeout=HTTP_SHORT_TIMEOUT) as resp:
            data = await resp.json()
            if not data.get("ok"):
                description = data.get("description")
                log.warning(f"editMessageText API error: {description}")
                return False, description
            return True, None
    except Exception as e:
        log.warning(f"editMessageText HTTP: {e}")
        return False, str(e)
async def _business_edit_message(conn_id: str, chat_id: int, msg_id: int, text: str, reply_markup=None) -> bool:
    ok, _ = await _business_edit_message_ex(conn_id, chat_id, msg_id, text, reply_markup=reply_markup)
    return ok
async def _business_send_message_ex(conn_id: str, chat_id: int, text: str, reply_markup=None, parse_mode: str | None = None) -> tuple[bool, int | None, str | None]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "business_connection_id": conn_id,
        "chat_id": chat_id,
        "text": text,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    try:
        session = get_http()
        async with session.post(url, json=payload, timeout=HTTP_SHORT_TIMEOUT) as resp:
            data = await resp.json()
            if not data.get("ok"):
                params = data.get("parameters") or {}
                retry_after = params.get("retry_after")
                description = data.get("description")
                log.warning(f"sendMessage API error: {description}")
                return False, retry_after, description
            return True, None, None
    except Exception as e:
        log.warning(f"sendMessage HTTP: {e}")
        return False, None, str(e)
async def _business_delete_message_ex(conn_id: str, msg_id: int) -> tuple[bool, int | None, str | None]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteBusinessMessages"
    payload = {
        "business_connection_id": conn_id,
        "message_ids": [msg_id],
    }
    try:
        session = get_http()
        async with session.post(url, json=payload, timeout=HTTP_SHORT_TIMEOUT) as resp:
            data = await resp.json()
            if not data.get("ok"):
                params = data.get("parameters") or {}
                retry_after = params.get("retry_after")
                description = data.get("description")
                log.warning(f"deleteBusinessMessages API error: {description}")
                return False, retry_after, description
            return True, None, None
    except Exception as e:
        log.warning(f"deleteBusinessMessages HTTP: {e}")
        return False, None, str(e)
