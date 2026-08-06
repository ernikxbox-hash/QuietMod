"""📊 .status — статистика переписки с пользователем.

Три режима (как у .info):
1. Бизнес-чат: .status (без аргумента) — статистика текущего чата
   с собеседником. С аргументом .status @username / .status id — статистика
   переписки с этим человеком (по всем чатам в архиве).
2. ЛС с ботом: .status @username / .status id — статистика по человеку.
3. Группа/канал: .status (ответь на сообщение) / .status @username /
   .status id — статистика по этому человеку.

Данные — из архива бота (таблица messages): сколько сообщений прислал
собеседник, сколько ответил владелец, разбивка по типам медиа, объём
текста, первое и последнее сообщение, сколько удалённых перехвачено.
"""
from html import escape as html_escape
from typing import Optional

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.types import Message

import database as db
from business_api import _business_edit_message, _get_owner_id_cached
from core import BOT_USERNAME, dp, log
from functions import LINE, resolve_username_to_chat

# Эмодзи для типов медиа (ключи — значения MEDIA_MAP из functions.py)
_MEDIA_EMOJI = {
    "◆ Фото":       "📷",
    "◆ Видео":      "🎬",
    "◆ Аудио":      "🎵",
    "◆ Голосовое":  "🎤",
    "◆ Документ":   "📄",
    "◆ Стикер":     "🏷",
    "◆ Кружок":     "🌀",
    "◆ GIF":        "🎞",
    "◆ Текст":      "💬",
}


def _fmt_num(n: int) -> str:
    """1 234 567 — разряды с пробелом."""
    return f"{n:,}".replace(",", " ")


def _status_card(st: dict, chat_name: str = "") -> str:
    """HTML-карточка статистики. chat_name — название чата, если известен."""
    # имена и названия чатов — пользовательские данные: экранируем HTML
    name = html_escape(st["name"] or "Неизвестно")
    chat_name = html_escape(chat_name)
    uname = (st["username"] or "").lstrip("@")
    header = "📊 <b>СТАТИСТИКА ПЕРЕПИСКИ</b>"
    lines = [header, f"<code>{LINE}</code>", ""]
    if chat_name:
        lines.append(f"◇ <b>Чат:</b> {chat_name}")
    lines.append(f"◇ <b>Собеседник:</b> {name}")
    if uname:
        lines.append(f"◇ <b>Username:</b> <a href=\"https://t.me/{uname}\">@{uname}</a>")
    lines.append("")
    total = st["peer"] + st["you"]
    lines.append(f"◇ <b>Всего сообщений:</b> {_fmt_num(total)}")
    lines.append(f"◇ Собеседник: <b>{_fmt_num(st['peer'])}</b> · Ты: <b>{_fmt_num(st['you'])}</b>")
    if st["chars"]:
        lines.append(f"◇ Текста: <b>{_fmt_num(st['chars'])}</b> симв.")
    media = st["media"]
    if media:
        lines.append(f"<code>{LINE}</code>")
        lines.append("◇ <b>Медиа:</b>")
        for mt, cnt in sorted(media.items(), key=lambda x: -x[1]):
            emoji = _MEDIA_EMOJI.get(mt, "◆")
            lines.append(f"   {emoji} {mt.replace('◆ ', '')} — {_fmt_num(cnt)}")
    if st["deleted"]:
        lines.append(f"   🗑 Поймано удалённых — {_fmt_num(st['deleted'])}")
    lines.append(f"<code>{LINE}</code>")
    if st["first"]:
        lines.append(f"◇ Первое: <code>{st['first']}</code>")
    if st["last"]:
        lines.append(f"◇ Последнее: <code>{st['last']}</code>")
    lines.append("")
    lines.append(f"— 👁️ @{BOT_USERNAME}")
    return "\n".join(lines)


_STATUS_HINT = (
    f"📊 <b>.status</b> — статистика переписки.\n"
    f"◇ В чате: <code>.status</code> — по текущему собеседнику.\n"
    f"◇ По человеку: <code>.status @username</code> или <code>.status id</code>\n\n"
    f"— 👁️ @{BOT_USERNAME}"
)


def _status_target_from_text(msg: Message) -> Optional[str]:
    """Аргумент команды: @username / числовой id / None."""
    raw = (msg.text or "").strip()
    body = raw[len(".status"):].strip() if raw.lower().startswith(".status") else ""
    return body.lstrip("@").strip() or None


async def _status_for(owner_id: int, target: str,
                      chat_name: str = "") -> tuple[Optional[str], Optional[str]]:
    """Считает статистику по цели: (карточка, ошибка-подсказка)."""
    if target.isdigit():
        peer_id = int(target)
        st = await db.chat_stats(owner_id, peer_id, chat_name=chat_name)
        if st is None:
            return None, "◇ <b>.status</b> — этого человека нет в архиве."
        return _status_card(st, chat_name=chat_name), None
    resolved = await resolve_username_to_chat(target)
    if not resolved:
        return None, "◇ <b>.status</b> — не удалось найти этого пользователя."
    peer_id = resolved.get("id")
    st = await db.chat_stats(owner_id, peer_id, chat_name=chat_name)
    if st is None:
        return None, "◇ <b>.status</b> — этого человека нет в архиве."
    return _status_card(st, chat_name=chat_name), None


# ── Бизнес-чат: .status — текущий собеседник / .status @user ──────────
@dp.business_message(F.text.regexp(r"(?i)^\.status(\s+.*)?$"))
async def on_status_business(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return
    owner_id = await _get_owner_id_cached(conn_id, ".status")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    target = _status_target_from_text(msg)
    chat_name = ""
    if not target:
        # без аргумента: в ЛС показываем собеседника, в группе — реплай
        if getattr(msg.chat, "type", None) == "private":
            peer_id = msg.chat.id
            if peer_id == owner_id:
                await _business_edit_message(conn_id, msg.chat.id, msg.message_id, _STATUS_HINT)
                return
            chat_name = getattr(msg.chat, "full_name", "") or getattr(msg.chat, "title", "") or ""
            card, err = await _status_for(owner_id, str(peer_id), chat_name=chat_name)
        elif msg.reply_to_message and msg.reply_to_message.from_user:
            peer_id = msg.reply_to_message.from_user.id
            chat_name = getattr(msg.chat, "title", "") or ""
            card, err = await _status_for(owner_id, str(peer_id), chat_name=chat_name)
        else:
            await _business_edit_message(conn_id, msg.chat.id, msg.message_id, _STATUS_HINT)
            return
    else:
        chat_name = getattr(msg.chat, "title", "") or ""
        card, err = await _status_for(owner_id, target, chat_name=chat_name)
    if card is None:
        await _business_edit_message(conn_id, msg.chat.id, msg.message_id, err)
        return
    await _business_edit_message(conn_id, msg.chat.id, msg.message_id, card)
    log.info(f"📊 .status business owner={owner_id} target={target or msg.chat.id}")


# ── ЛС с ботом: .status @username / .status id ────────────────────────
@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.status(\s+.+)?$"), F.chat.type == "private")
async def on_status_dm(msg: Message):
    if not msg.from_user:
        return
    target = _status_target_from_text(msg)
    if not target:
        await msg.answer(_STATUS_HINT)
        return
    card, err = await _status_for(msg.from_user.id, target)
    if card is None:
        await msg.answer(err)
        return
    await msg.answer(card)
    log.info(f"📊 .status dm user={msg.from_user.id} target={target}")


# ── Группа / канал: .status (реплай / @user / id) ─────────────────────
@dp.message(F.text.regexp(r"(?i)^\.status(\s+.*)?$"),
            F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_status_group(msg: Message):
    if not msg.from_user:
        return
    await db.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    target = _status_target_from_text(msg)
    chat_name = getattr(msg.chat, "title", "") or ""
    if not target:
        if msg.reply_to_message and msg.reply_to_message.from_user:
            target = str(msg.reply_to_message.from_user.id)
        else:
            await msg.reply(_STATUS_HINT)
            return
    card, err = await _status_for(msg.from_user.id, target, chat_name=chat_name)
    if card is None:
        await msg.reply(err)
        return
    await msg.reply(card)
    log.info(f"📊 .status group chat={msg.chat.id} target={target}")
