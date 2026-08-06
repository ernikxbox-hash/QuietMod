"""🎯 .who — «Кто вероятнее…»: рандомный участник чата на вопрос.

Механика:
- .who кто самый добрый — бот выбирает случайного участника из кэша
  участников чата (тот же кэш, что у .knb: пополняется при каждом
  сообщении в группе и бизнес-группе).
- Работает в обычных группах (бот в чате) и бизнес-группах.
- Вопрос необязателен: .who без текста покажет подсказку.
- Без внешних API — чистый рандом, ломаться нечему.
"""
import random
import re
from typing import Optional

from aiogram import F
from aiogram.types import Message
from html import escape as html_escape

from business_api import _business_edit_message, _get_owner_id_cached
from core import dp
from functions import LINE
from handlers_games import get_group_members


def _who_title(raw: str) -> str:
    """«кто самый добрый?» → «Самый добрый» (заголовок ответа)."""
    t = raw.strip().strip("?!").strip()
    t = re.sub(r"(?i)^кто\s+", "", t).strip()
    t = re.sub(r"(?i)^вероятнее\s+", "", t).strip()
    return (t[:60] or "Сюрприз").capitalize()


def _who_name(m: dict) -> str:
    if m.get("username"):
        return f"@{html_escape(m['username'])}"
    return html_escape(m.get("full_name") or "Участник")


def _who_pick(chat_id: int, exclude_uid: Optional[int]) -> Optional[dict]:
    """Случайный участник из кэша чата (автора не выбираем, если есть кто-то ещё)."""
    members = [m for m in get_group_members(chat_id) if m.get("id")]
    if exclude_uid:
        others = [m for m in members if m["id"] != exclude_uid]
        if others:
            members = others
    return random.choice(members) if members else None


def _who_result(title: str, pick: dict, total: int) -> str:
    return (
        f"◆ <b>{html_escape(title.upper())}</b>\n"
        f"<code>{LINE}</code>\n\n"
        f"◇ В этом чате — <b>{_who_name(pick)}</b>\n"
        f"◇ Выбор из {total} участников"
    )


_WHO_HINT = (
    f"◆ <b>WHO</b> · кто вероятнее\n"
    f"<code>{LINE}</code>\n\n"
    "◇ Напиши так: <code>.who кто самый добрый</code>\n"
    "◇ Или: <code>.who кто вероятнее купит пиццу</code>\n\n"
    f"<code>{LINE}</code>\n"
    "◇ Бот выберет случайного участника чата.\n"
    "◇ Чем активнее чат — тем больше имён в пуле."
)


# ── Группа / супергруппа (обычный бот) ────────────────────────────────
@dp.message(F.text.regexp(r"(?i)^\.who(\s+.*)?$"), F.chat.type.in_({"group", "supergroup"}))
async def on_who_group(msg: Message):
    if not msg.from_user:
        return
    raw = (msg.text or "").strip()[len(".who"):].strip()
    if not raw:
        await msg.reply(_WHO_HINT)
        return
    title = _who_title(raw)
    total = len(get_group_members(msg.chat.id))
    pick = _who_pick(msg.chat.id, msg.from_user.id)
    if pick is None:
        await msg.reply(
            f"◆ <b>{html_escape(title.upper())}</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Пока не из кого выбирать — в чате тихо.\n"
            "◇ Пусть участники напишут пару слов, и попробуй снова."
        )
        return
    await msg.reply(_who_result(title, pick, total))


# ── Бизнес-группа (владелец подключения) ──────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.who(\s+.*)?$"))
async def on_who_business(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return
    if getattr(msg.chat, "type", None) not in ("group", "supergroup"):
        return
    owner_id = await _get_owner_id_cached(conn_id, ".who")
    if owner_id is None:
        return
    raw = (msg.text or "").strip()[len(".who"):].strip()
    if not raw:
        text = _WHO_HINT
    else:
        title = _who_title(raw)
        total = len(get_group_members(msg.chat.id))
        pick = _who_pick(msg.chat.id, msg.from_user.id if msg.from_user else None)
        if pick is None:
            text = (
                f"◆ <b>{html_escape(title.upper())}</b>\n"
                f"<code>{LINE}</code>\n\n"
                "◇ Пока не из кого выбирать — в чате тихо.\n"
                "◇ Пусть участники напишут пару слов, и попробуй снова."
            )
        else:
            text = _who_result(title, pick, total)
    await _business_edit_message(conn_id, msg.chat.id, msg.message_id, text)
