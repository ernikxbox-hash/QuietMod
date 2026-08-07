"""🛡 Глобальный обработчик ошибок: ни одно падение хендлера не проходит мимо.

Ловит ВСЕ исключения из хендлеров через dp.errors():
- логирует с трейсбеком;
- шлёт компактный репорт админу (не чаще 1 раза в минуту на ту же ошибку);
- юзеру — вежливое «не получилось» (не чаще 1 раза в минуту на чат).

Безобидные API-ошибки (message is not modified, flood limit, сетевые сбои)
молча пропускаются — это нормальная жизнь бота, а не поломки.
"""
import time
from html import escape as html_escape

from aiogram.types import ErrorEvent
from core import ADMIN_ID, BOT_USERNAME, bot, dp, log

_BENIGN_MARKERS = (
    "message is not modified",
    "message to edit not found",
    "there is no message",
    "message can't be deleted",
    "message identifier is not specified",
    "query is too old",
    "callback query is too old",
    "chat not found",
    "user not found",
    "bot was blocked by the user",
    "bot was kicked",
    "user is deactivated",
    "can't parse entities",
    "bad request: not enough rights",
    "method is available only for supergroups",
    "chat member status can't be changed",
    # флуд-лимиты и сетевые сбои — транзиентные, спамить репортами не нужно
    "retry after",
    "flood control exceeded",
    "connection reset",
    "connection aborted",
    "remote end closed connection",
    "server disconnected",
    "connectionerror",
    "clientconnectorerror",
    "clientresponseerror",
    "clientpayloaderror",
    "clientconnectionerror",
    "timeout",
    "timed out",
    "gaierror",
    "dns",
    "ssl",
)

_admin_last: dict[tuple[str, str], float] = {}  # (тип, хвост текста) -> время репорта
_user_last: dict[int, float] = {}               # chat_id -> время ответа юзеру


def _is_benign(e: Exception) -> bool:
    s = f"{type(e).__name__} {e}".lower()
    return any(m in s for m in _BENIGN_MARKERS)


def _first_message(update):
    """Сообщение из апдейта, в котором что-то упало (для ответа юзеру)."""
    if update is None:
        return None
    msg = getattr(update, "message", None) or getattr(update, "edited_message", None)
    if msg is not None:
        return msg
    msg = getattr(update, "business_message", None) or getattr(update, "edited_business_message", None)
    if msg is not None:
        return msg
    cb = getattr(update, "callback_query", None)
    if cb is not None:
        return getattr(cb, "message", None)
    return None


@dp.errors()
async def on_aiogram_error(event: ErrorEvent) -> None:
    e = event.exception
    if e is None:
        return
    if _is_benign(e):
        log.debug(f"ℹ benign: {type(e).__name__}: {str(e)[:120]}")
        return
    log.error(f"⚠️ Handler error: {type(e).__name__}: {e}", exc_info=e)
    now = time.monotonic()

    # ── Репорт админу (1 раз в минуту на одну и ту же ошибку) ─────────
    key = (type(e).__name__, str(e)[:60].lower())
    if now - _admin_last.get(key, 0.0) >= 60:
        _admin_last[key] = now
        if ADMIN_ID:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    "⚠️ <b>Ошибка в боте</b>\n"
                    f"<code>{html_escape(type(e).__name__)}</code>\n"
                    f"<code>{html_escape(str(e)[:300])}</code>",
                )
            except Exception:
                pass

    # ── Вежливое «не получилось» юзеру (1 раз в минуту на чат) ────────
    msg = _first_message(getattr(event, "update", None))
    if msg is not None and getattr(msg, "chat", None) is not None:
        chat_id = msg.chat.id
        if now - _user_last.get(chat_id, 0.0) >= 60:
            _user_last[chat_id] = now
            try:
                await msg.answer(
                    "◇ <b>Не получилось</b> — что-то пошло не так.\n"
                    f"◇ Попробуй ещё раз. Если повторится — напиши @{BOT_USERNAME}"
                )
            except Exception:
                pass
