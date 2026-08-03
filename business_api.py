import aiohttp
from core import BOT_TOKEN, get_http, log

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
        session = get_http()
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
        session = get_http()
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
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
