"""🎯 .who — «Кто вероятнее…»: рандомный участник чата на вопрос.

Механика:
- .who кто самый добрый — бот выбирает случайного участника чата.
- Пул участников собирается из ЧЕТЫРЁХ источников (чем больше, тем
  честнее рандом): кэш сообщений (.knb), уровни из БД, админы чата
  через Bot API и полный список участников через MTProto (если настроен
  TELEGRAM_API_ID/HASH). Автор команды НЕ исключается — все равны.
- Работает в обычных группах (бот в чате) и бизнес-группах.
- Вопрос необязателен: .who без текста покажет подсказку.
- Любой сбой источника молча пропускается — бот не падает.
"""
import asyncio
import random
import re
from typing import Optional

from aiogram import F
from aiogram.types import Message
from html import escape as html_escape

import database as db
from business_api import _business_edit_message, _get_owner_id_cached
from core import bot, dp
from functions import LINE
from handlers_games import get_group_members
from mtproto_resolver import get_chat_participants_mtproto


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


async def _who_pool(chat_id: int) -> list[dict]:
    """Объединяем всех, кого знаем в чате, из четырёх источников.

    Bot API не умеет получать полный список участников группы, поэтому пул
    собирается из: кэш сообщений + уровни из БД + админы (Bot API) +
    полный список участников (MTProto, если настроен). Автор команды НЕ
    исключается: рандом честный, все равны.
    """
    # Кэш сообщений — мгновенный, кладём сразу; медленные источники (БД,
    # Bot API, MTProto) запускаем ПАРАЛЛЕЛЬНО через gather — чтобы .who
    # отвечал быстро, а не ждал каждый источник по очереди (~1-2 сек).
    pool: dict[int, dict] = {}
    for m in get_group_members(chat_id):
        if m.get("id"):
            pool[m["id"]] = m

    async def _db_levels():
        try:
            return await db.get_chat_level_users(chat_id)
        except Exception:
            return []

    async def _admins():
        try:
            return await bot.get_chat_administrators(chat_id)
        except Exception:
            return []

    async def _mtproto():
        try:
            return (await get_chat_participants_mtproto(chat_id)) or []
        except Exception:
            return []

    levels, admins, mtproto = await asyncio.gather(_db_levels(), _admins(), _mtproto())
    for r in levels:
        pool[r["user_id"]] = {
            "id": r["user_id"],
            "username": (r.get("username") or "").lstrip("@"),
            "full_name": r.get("name") or "",
        }
    for cm in admins:
        u = getattr(cm, "user", None)
        if u and not getattr(u, "is_bot", False):
            pool[u.id] = {"id": u.id, "username": u.username or "", "full_name": u.full_name or ""}
    for m in mtproto:
        pool[m["id"]] = m
    return list(pool.values())


def _who_pick(pool: list[dict]) -> Optional[dict]:
    return random.choice(pool) if pool else None


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
    "◇ В пуле: кто писал в чате, админы и все с уровнем."
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
    pool = await _who_pool(msg.chat.id)
    title = _who_title(raw)
    total = len(pool)
    pick = _who_pick(pool)
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
        pool = await _who_pool(msg.chat.id)
        title = _who_title(raw)
        total = len(pool)
        pick = _who_pick(pool)
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
