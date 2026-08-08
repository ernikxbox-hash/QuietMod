"""◇ Неизвестные команды — подсказка вместо тишины.

Написал `.фыва` — бот вежливо скажет, что такой команды нет, и покажет .cmd.

Правила:
- Реагируем только на сообщения, начинающиеся с `.слово` (точка + слово).
- Известные команды исключаем на уровне РЕГЕКСА (negative lookahead), а не
  в теле хендлера: в aiogram первый подходящий хендлер выигрывает, и если
  наш регекс сматчит известную команду (.cmd, .help, .ai и т.д.), событие
  съест этот хендлер, а настоящий (из handlers_commands / handlers_admin)
  не вызовется вовсе.
- В бизнес-чатах подсказку видит только владелец подключения.
- Чтобы не спамить — не чаще раза в 30 секунд на чат.
- Модуль подключается ДО catch-all перехватчика (handlers_intercept),
  поэтому неизвестные команды не попадают в архив.
"""
import re
import time
from html import escape as html_escape

from aiogram import F
from aiogram.types import Message
from business_api import _business_send_message_ex, _get_owner_id_cached
from core import dp, log

# Все известные команды (первое слово после точки). Для них подсказку НЕ шлём.
_KNOWN_NAMES = {
    "ai", "spam", "price", "curs", "mute", "unmute", "nomute", "unnomute",
    "afk", "unafk", "code", "uncode", "wbl", "unwbl", "black", "unblack",
    "cmd", "help", "knb", "level", "unlevel", "who", "ramka", "stik", "krom",
    "voice", "wm", "gif", "audio", "шрифт", "info", "sled", "unsled", "infosled",
    "bold", "italic", "mono", "line", "crossed", "hidden", "quote", "recap",
}

# Известные команды исключаем на уровне регекса — см. докстринг модуля.
_KNOWN_ALT = "|".join(re.escape(n) + r"\b" for n in sorted(_KNOWN_NAMES, key=len, reverse=True))
_CMD_PATTERN = rf"(?is)^\.(?!{_KNOWN_ALT})([a-zа-яё0-9_]{{1,32}})\b"
_CMD_RE = re.compile(_CMD_PATTERN)
_HINT_COOLDOWN: dict[int, float] = {}


def _cmd_word(text: str) -> str | None:
    m = _CMD_RE.match(text or "")
    return m.group(1).lower() if m else None


def _hint_text(word: str) -> str:
    return (
        f"◇ Не знаю команду <code>.{html_escape(word)}</code>\n"
        "◇ Все команды бота — <code>.cmd</code>"
    )


async def _reply_hint(msg: Message, word: str) -> None:
    chat_id = msg.chat.id
    now = time.monotonic()
    if now - _HINT_COOLDOWN.get(chat_id, 0.0) < 30:
        return
    _HINT_COOLDOWN[chat_id] = now
    try:
        if msg.business_connection_id:
            await _business_send_message_ex(
                msg.business_connection_id, chat_id, _hint_text(word), parse_mode="HTML"
            )
        else:
            await msg.reply(_hint_text(word))
    except Exception as e:
        log.debug(f"unknown hint reply: {e}")


# ── Бизнес-чаты (подсказку видит только владелец) ─────────────────────
@dp.business_message(F.text.regexp(_CMD_PATTERN))
async def on_unknown_business(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id or not msg.from_user:
        return
    owner_id = await _get_owner_id_cached(conn_id, ".unknown")
    if owner_id is None or msg.from_user.id != owner_id:
        return
    word = _cmd_word(msg.text or "")
    if not word:
        return
    await _reply_hint(msg, word)


# ── Группы / супергруппы ──────────────────────────────────────────────
@dp.message(F.text.regexp(_CMD_PATTERN), F.chat.type.in_({ "group", "supergroup" }))
async def on_unknown_group(msg: Message):
    if not msg.from_user:
        return
    word = _cmd_word(msg.text or "")
    if not word:
        return
    await _reply_hint(msg, word)
