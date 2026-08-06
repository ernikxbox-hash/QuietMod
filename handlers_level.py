"""⬆ .level — уровни и XP за активность в чатах.

Механика:
- Каждое сообщение в группе/супергруппе даёт XP: текст 1, длинный текст
  (40+ симв.) +1, медиа +2, ответ кому-то +1. Команды XP не дают.
- Анти-фарм: XP начисляется не чаще раза в 15 секунд на юзера в чате.
- Кривая уровней: 100, 250, 450, 700, 1000, 1350, ... (нелинейная).
- При повышении уровня — ОДНА маленькая строка «◇ @user → уровень N»,
  не чаще раза в 20 секунд на чат (чтобы не заёбывало, но было видно).
- .level — карточка уровня · .level top — топ-10 чата · .level @user / ответ — чужой.
- .unlevel — админ выключает уровни · .level от админа при выключенных — включает.

Работает в обычных группах (бот в чате) и бизнес-группах (через Business API).
"""
import asyncio
import math
import time
from typing import Optional

from aiogram import F
from aiogram.types import Message
from html import escape as html_escape

import database as db
from business_api import (
    _business_edit_message,
    _business_send_message_ex,
    _get_owner_id_cached,
)
from core import ADMIN_ID, bot, dp, log
from functions import LINE

_XP_ANTIFARM_SECONDS = 15.0    # не чаще раза в 15 сек на юзера в чате
_ANNOUNCE_QUIET_SECONDS = 20.0 # не чаще 1 анонса в 20 сек на чат

_last_xp_at: dict[tuple[int, int], float] = {}
_last_announce_at: dict[int, float] = {}
_level_enabled_cache: dict[int, tuple[bool, float]] = {}


# ── Кривая уровней ────────────────────────────────────────────────────
# xp(level) = 100·(L-1) + 25·(L-1)·(L-2):  100, 250, 450, 700, 1000, ...
def _cumulative_xp(level: int) -> int:
    if level <= 1:
        return 0
    return 100 * (level - 1) + 25 * (level - 1) * (level - 2)


def _level_from_xp(xp: int) -> int:
    if xp <= 0:
        return 1
    return int((math.sqrt(5625 + 100 * xp) - 25) / 50)


def _next_level_xp(level: int) -> int:
    return _cumulative_xp(level + 1) - _cumulative_xp(level)


_LEVEL_TITLES = [
    (1,  "Новичок"),
    (2,  "Разговорчивый"),
    (3,  "Активный"),
    (4,  "Свой в доску"),
    (5,  "Тусовщик"),
    (6,  "Завсегдатай"),
    (7,  "Душа компании"),
    (8,  "Ветеран"),
    (9,  "Легенда чата"),
    (10, "Бог чата"),
    (12, "Абсолют"),
]


def _level_title(level: int) -> str:
    t = "Новичок"
    for lv, title in _LEVEL_TITLES:
        if level >= lv:
            t = title
    return t


def _progress_bar(filled: int, total: int = 10) -> str:
    filled = max(0, min(total, filled))
    return "▰" * filled + "▱" * (total - filled)


def _user_link(user) -> str:
    uname = (user.username or "").strip()
    if uname:
        return f"@{html_escape(uname)}"
    return html_escape(user.full_name or "—") or "—"


async def _is_chat_admin(chat_id: int, uid: int) -> bool:
    if uid == ADMIN_ID:
        return True
    try:
        m = await bot.get_chat_member(chat_id, uid)
        return m.status in ("creator", "administrator")
    except Exception:
        return False


# ── Кэш включённости уровней (не дёргаем БД на каждое сообщение) ─────
async def _level_enabled(chat_id: int) -> bool:
    now = time.monotonic()
    cached = _level_enabled_cache.get(chat_id)
    if cached and now - cached[1] < 60:
        return cached[0]
    enabled = await db.is_level_enabled(chat_id)
    _level_enabled_cache[chat_id] = (enabled, now)
    if len(_level_enabled_cache) > 20_000:
        _level_enabled_cache.clear()
    return enabled


def _invalidate_level(chat_id: int):
    _level_enabled_cache.pop(chat_id, None)


# ── Начисление XP (вызывается из catch-all групп и бизнес-чатов) ──────
async def award_chat_xp(chat_id: int, user, msg: Message, conn_id: Optional[str] = None) -> None:
    if not user or not getattr(user, "id", None) or getattr(user, "is_bot", False):
        return
    text = msg.text or msg.caption or ""
    stripped = text.strip()
    if stripped.startswith(".") or stripped.startswith("/"):
        return
    # Service-сообщения (join/leave/rename и т.п.) XP не дают: нет ни текста,
    # ни подписи, ни медиа — только служебные поля.
    if not stripped and not any(getattr(msg, a, None) for a in (
        "photo", "video", "video_note", "voice", "animation", "sticker", "document", "audio"
    )):
        return
    now = time.monotonic()
    key = (chat_id, user.id)
    if now - _last_xp_at.get(key, 0.0) < _XP_ANTIFARM_SECONDS:
        return
    if not await _level_enabled(chat_id):
        return
    _last_xp_at[key] = now
    if len(_last_xp_at) > 50_000:
        _last_xp_at.clear()
    amount = 1
    if len(stripped) > 40:
        amount += 1
    if any(getattr(msg, a, None) for a in (
        "photo", "video", "video_note", "voice", "animation", "sticker", "document", "audio"
    )):
        amount += 2
    if msg.reply_to_message and msg.reply_to_message.from_user:
        amount += 1
    new_xp = await db.add_chat_xp(
        chat_id, user.id, amount,
        (user.full_name or "")[:64], (user.username or "")[:32],
    )
    new_level = _level_from_xp(new_xp)
    old_level = _level_from_xp(max(0, new_xp - amount))
    if new_level > old_level:
        await _announce_level_up(chat_id, user, new_level, conn_id)


async def _announce_level_up(chat_id: int, user, level: int, conn_id: Optional[str] = None):
    now = time.monotonic()
    if now - _last_announce_at.get(chat_id, 0.0) < _ANNOUNCE_QUIET_SECONDS:
        return
    _last_announce_at[chat_id] = now
    if len(_last_announce_at) > 10_000:
        _last_announce_at.clear()
    text = f"◇ {_user_link(user)} → уровень <b>{level}</b> · {_level_title(level)}"
    try:
        if conn_id:
            ok, retry_after, _ = await _business_send_message_ex(conn_id, chat_id, text, parse_mode="HTML")
            if not ok and retry_after:
                await asyncio.sleep(int(retry_after))
                await _business_send_message_ex(conn_id, chat_id, text, parse_mode="HTML")
        else:
            await bot.send_message(chat_id, text)
    except Exception as e:
        log.debug(f"level announce: {e}")


# ── Отрисовка карточек ────────────────────────────────────────────────
async def _level_card(chat_id: int, uid: int) -> Optional[str]:
    row = await db.get_chat_level(chat_id, uid)
    if not row:
        return None
    xp = row["xp"]
    level = _level_from_xp(xp)
    in_level = xp - _cumulative_xp(level)
    need = _next_level_xp(level)
    rank = await db.get_chat_rank(chat_id, uid)
    uname = f"@{row['username']}" if row.get("username") else html_escape(row.get("name") or "—")
    cells = int(10 * in_level / need) if need > 0 else 0
    return (
        f"◆ <b>LEVEL {level}</b> · {_level_title(level)}\n"
        f"<code>{LINE}</code>\n\n"
        f"◇ <b>{uname}</b>\n"
        f"◇ XP: <b>{in_level}</b> / {need}\n"
        f"◇ {_progress_bar(cells)}\n"
        f"◇ Место в чате: <b>#{rank}</b>\n"
        f"◇ До уровня {level + 1}: {need - in_level} XP\n\n"
        f"<code>{LINE}</code>\n"
        f"◇ <code>.level top</code> — топ чата"
    )


async def _top_card(chat_id: int) -> str:
    top = await db.get_chat_top(chat_id, 10)
    if not top:
        return (
            f"◆ <b>ТОП ЧАТА</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Пока пусто — напиши первое сообщение!"
        )
    lines = [f"◆ <b>ТОП ЧАТА</b>", f"<code>{LINE}</code>"]
    for i, r in enumerate(top, start=1):
        lvl = _level_from_xp(r["xp"])
        uname = f"@{r['username']}" if r.get("username") else html_escape(r.get("name") or "—")
        marker = "◆" if i == 1 else "◇"
        lines.append(f"{marker} {i}. <b>{uname}</b> · ур. <b>{lvl}</b> · {r['xp']} XP")
    lines.append(f"<code>{LINE}</code>")
    return "\n".join(lines)


async def _resolve_target_uid(msg: Message, chat_id: int, args: str, default_uid: int) -> int:
    """Кого показываем: по умолчанию себя, по ответу — собеседника, по @username — из БД."""
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id
    if args.startswith("@"):
        row = await db.get_chat_level_by_username(chat_id, args.lstrip("@").strip())
        if row:
            return row["user_id"]
    return default_uid


# ── Команды: обычные группы ───────────────────────────────────────────
@dp.message(F.text.regexp(r"(?i)^\.level(\s+.*)?$"), F.chat.type.in_({"group", "supergroup"}))
async def on_level_cmd(msg: Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    chat_id = msg.chat.id
    args = (msg.text or "").strip()[len(".level"):].strip().lower()
    if not await _level_enabled(chat_id):
        if await _is_chat_admin(chat_id, uid):
            await db.set_level_enabled(chat_id, True)
            _invalidate_level(chat_id)
        else:
            await msg.reply(
                f"◇ <b>Уровни выключены</b> в этом чате.\n"
                f"◇ Админ может включить: <code>.level</code>"
            )
            return
    if args.startswith("top"):
        await msg.reply(await _top_card(chat_id))
        return
    target_uid = await _resolve_target_uid(msg, chat_id, args, uid)
    card = await _level_card(chat_id, target_uid)
    await msg.reply(card or "◇ У этого участника пока нет уровня в этом чате.")


@dp.message(F.text.regexp(r"(?i)^\.unlevel$"), F.chat.type.in_({"group", "supergroup"}))
async def on_unlevel_cmd(msg: Message):
    if not msg.from_user:
        return
    if not await _is_chat_admin(msg.chat.id, msg.from_user.id):
        return
    await db.set_level_enabled(msg.chat.id, False)
    _invalidate_level(msg.chat.id)
    await msg.reply(
        f"◇ <b>Уровни выключены</b> в этом чате.\n"
        f"◇ Включить обратно: <code>.level</code>"
    )


# ── Команды: бизнес-группы (владелец) ─────────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.level(\s+.*)?$"))
async def on_level_business(msg: Message):
    if not msg.business_connection_id:
        return
    if getattr(msg.chat, "type", None) not in ("group", "supergroup"):
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".level")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    conn_id = msg.business_connection_id
    chat_id = msg.chat.id
    args = (msg.text or "").strip()[len(".level"):].strip().lower()
    if not await _level_enabled(chat_id):
        await db.set_level_enabled(chat_id, True)
        _invalidate_level(chat_id)
    if args.startswith("top"):
        text = await _top_card(chat_id)
    else:
        target_uid = await _resolve_target_uid(msg, chat_id, args, owner_id)
        card = await _level_card(chat_id, target_uid)
        text = card or "◇ У этого участника пока нет уровня в этом чате."
    await _business_edit_message(conn_id, chat_id, msg.message_id, text)


@dp.business_message(F.text.regexp(r"(?i)^\.unlevel$"))
async def on_unlevel_business(msg: Message):
    if not msg.business_connection_id:
        return
    if getattr(msg.chat, "type", None) not in ("group", "supergroup"):
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".unlevel")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    await db.set_level_enabled(msg.chat.id, False)
    _invalidate_level(msg.chat.id)
    await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        f"◇ <b>Уровни выключены</b> в этом чате.\n◇ Включить обратно: <code>.level</code>",
    )
