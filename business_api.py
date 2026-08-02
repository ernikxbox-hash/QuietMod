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


async def _business_send_message_ex(conn_id: str, chat_id: int, text: str) -> tuple[bool, int | None, str | None]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "business_connection_id": conn_id,
        "chat_id": chat_id,
        "text": text,
    }
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
