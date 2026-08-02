import asyncio
import aiohttp
from core import BOT_TOKEN, log

# Единая персистентная HTTP-сессия (keep-alive).
# Раньше каждая операция создавала новую ClientSession -> новый TCP+TLS хендшейк.
# Для мута критична скорость: общая сессия переиспользует живое соединение.
_http_session: aiohttp.ClientSession | None = None


def _session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def _close_http() -> None:
    """Закрыть общую сессию при завершении бота."""
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
        _http_session = None


async def _tg_send_message(uid: int, text: str, reply_markup: dict, reply_to_message_id: int | None = None) -> int | None:
    """Отправка обычного сообщения с кастомными иконками на кнопках (raw Bot API).

    Нужно для icon_custom_emoji_id (Bot API 9.4) — старый aiogram его не знает.
    Возвращает message_id или None при ошибке."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": uid,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": reply_markup,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    try:
        session = _session()
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
            if not data.get("ok"):
                log.warning(f"sendMessage(icon) API error: {data.get('description')}")
                return None
            result = data.get("result") or {}
            return result.get("message_id")
    except Exception as e:
        log.warning(f"sendMessage(icon) HTTP: {e}")
        return None


async def _tg_edit_message(uid: int, msg_id: int, text: str, reply_markup: dict) -> bool:
    """editMessageText с кастомными иконками на кнопках (raw Bot API)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": uid,
        "message_id": msg_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": reply_markup,
    }
    try:
        session = _session()
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
            if not data.get("ok"):
                log.warning(f"editMessageText(icon) API error: {data.get('description')}")
                return False
            return True
    except Exception as e:
        log.warning(f"editMessageText(icon) HTTP: {e}")
        return False


async def _business_edit_message_ex(conn_id: str, chat_id: int, msg_id: int, text: str, reply_markup=None) -> tuple[bool, str | None]:
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
        session = _session()
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
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


async def _business_send_message_ex(conn_id: str, chat_id: int, text: str, reply_markup: dict | None = None, parse_mode: str | None = None) -> tuple[bool, int | None, str | None]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "business_connection_id": conn_id,
        "chat_id": chat_id,
        "text": text,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        session = _session()
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
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


async def _business_copy_message(conn_id: str, chat_id: int, from_chat_id: int, message_id: int) -> tuple[bool, int | None, str | None]:
    """copyMessage через бизнес-подключение: копия уходит от имени бизнес-аккаунта.

    Копия, отправленная ботом, невидима другим ботам (Telegram не доставляет
    сообщения от ботов другим ботам) — значит, чужой мут её не увидит и не удалит.
    Возвращает (ok, новый message_id, ошибка).
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/copyMessage"
    payload = {
        "business_connection_id": conn_id,
        "chat_id": chat_id,
        "from_chat_id": from_chat_id,
        "message_id": message_id,
    }
    try:
        session = _session()
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
            if not data.get("ok"):
                description = data.get("description")
                log.warning(f"copyMessage API error: {description}")
                return False, None, description
            result = data.get("result") or {}
            return True, result.get("message_id"), None
    except Exception as e:
        log.warning(f"copyMessage HTTP: {e}")
        return False, None, str(e)


async def _business_send_photo_ex(conn_id: str, chat_id: int, file_id: str, caption: str = "") -> tuple[bool, int | None, str | None]:
    """sendPhoto через бизнес-подключение (фолбэк анти-мута для фото)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    payload = {
        "business_connection_id": conn_id,
        "chat_id": chat_id,
        "photo": file_id,
    }
    if caption:
        payload["caption"] = caption
    try:
        session = _session()
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
            if not data.get("ok"):
                description = data.get("description")
                log.warning(f"sendPhoto API error: {description}")
                return False, None, description
            result = data.get("result") or {}
            return True, result.get("message_id"), None
    except Exception as e:
        log.warning(f"sendPhoto HTTP: {e}")
        return False, None, str(e)


async def _business_send_chat_action(conn_id: str, chat_id: int, action: str = "typing") -> bool:
    """sendChatAction через бизнес-подключение: индикатор показывается от имени
    бизнес-аккаунта (как будто печатает сам владелец), а не от имени бота.
    Индикатор живёт ~5 секунд — для длительного показа нужно переотправлять."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendChatAction"
    payload = {
        "business_connection_id": conn_id,
        "chat_id": chat_id,
        "action": action,
    }
    try:
        session = _session()
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if not data.get("ok"):
                log.warning(f"sendChatAction API error: {data.get('description')}")
                return False
            return True
    except Exception as e:
        log.warning(f"sendChatAction HTTP: {e}")
        return False


async def _business_delete_message_ex(conn_id: str, msg_id: int) -> tuple[bool, int | None, str | None]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteBusinessMessages"
    payload = {
        "business_connection_id": conn_id,
        "message_ids": [msg_id],
    }
    try:
        session = _session()
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if not data.get("ok"):
                params = data.get("parameters") or {}
                retry_after = params.get("retry_after")
                description = data.get("description")
                if retry_after:
                    log.debug(f"deleteBusinessMessages rate-limit: {description}")
                else:
                    log.warning(f"deleteBusinessMessages API error: {description}")
                return False, retry_after, description
            return True, None, None
    except Exception as e:
        log.warning(f"deleteBusinessMessages HTTP: {e}")
        return False, None, str(e)


async def _business_delete_retry(conn_id: str, msg_id: int, attempts: int = 3) -> bool:
    """Удаление с ретраями: 429 -> retry_after (кап 5с), сетевая ошибка -> короткий бэкофф."""
    for i in range(attempts):
        ok, retry_after, _ = await _business_delete_message_ex(conn_id, msg_id)
        if ok:
            return True
        if retry_after:
            await asyncio.sleep(min(int(retry_after), 5))
        else:
            await asyncio.sleep(0.12 * (i + 1))
    return False
