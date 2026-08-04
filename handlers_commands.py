"""Все точечные команды: .spam .mute .nomute .afk .code .wbl .ai .price .curs и форматирование."""
import asyncio
import re
import time
from datetime import date, datetime, timezone
from typing import Optional

import aiohttp

from aiogram import F
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from html import escape as html_escape

import database as db
from business_api import (
    _business_delete_message_ex,
    _business_edit_message,
    _business_send_message_ex,
    _get_owner_id_cached,
)
from core import BOT_TOKEN, BOT_USERNAME, bot, dp, get_http, log
from functions import (
    LINE,
    _POPULAR_CURS,
    _business_edit_ai_html,
    _edit_ai_html,
    _fmt_curs_rate,
    _get_curs_cached,
    _price_estimate,
    _reply_ai_html,
    _send_code_files,
    business_afk,
    business_afk_last_reply,
    business_code_mode,
    business_muted_chats,
    business_nomute_chats,
    business_spam_tasks,
    business_wbl_chats,
    groq_chat,
    spam_tasks,
    user_afk,
)


# ── .spam (Business) ───────────────────────────────────────────────────
async def _business_spam_worker(conn_id: str, chat_id: int, owner_id: int, text: str, count: int):
    key = (conn_id, chat_id, owner_id)
    try:
        for _ in range(count):
            ok, retry_after, _ = await _business_send_message_ex(conn_id, chat_id, text)
            if not ok:
                if retry_after:
                    await asyncio.sleep(int(retry_after))
                    continue
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error(f"business spam worker: {e}")
    finally:
        business_spam_tasks.pop(key, None)

@dp.business_message(F.text.regexp(r"(?i)^\.spam(\s+.+)?$"))
async def on_spam_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".spam")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    raw_text = (msg.text or msg.caption or "").strip()
    body = raw_text[5:].strip() if len(raw_text) >= 5 else ""
    key = (msg.business_connection_id, msg.chat.id, owner_id)
    if body.lower() == "stop":
        task = business_spam_tasks.get(key)
        if task and not task.done():
            task.cancel()
            await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Спам остановлен")
        else:
            business_spam_tasks.pop(key, None)
            await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Спам не запущен")
        return
    if not body or " " not in body:
        await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Формат: .spam текст 10  |  .spam stop")
        return
    text_part, count_part = body.rsplit(" ", 1)
    try:
        count = int(count_part)
    except Exception:
        await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Количество должно быть числом: .spam текст 10")
        return
    if count <= 0:
        await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Количество должно быть > 0")
        return
    existing = business_spam_tasks.get(key)
    if existing and not existing.done():
        stop_kb = {"inline_keyboard": [[{"text": "⛔ Остановить спам", "callback_data": "spam_stop_btn"}]]}
        await _business_edit_message(
            msg.business_connection_id, msg.chat.id, msg.message_id,
            (
                "🔊 <b>СПАМ УЖЕ ИДЁТ</b>\n"
                f"<code>{LINE}</code>\n\n"
                "◇ Рассылка уже запущена\n\n"
                f"<code>{LINE}</code>\n"
                "◇ Остановить: кнопка ниже 👇\n"
                "   или команда <code>.spam stop</code>\n\n"
                f"— 👁️ @{BOT_USERNAME}"
            ),
            reply_markup=stop_kb,
        )
        return
    business_spam_tasks[key] = asyncio.create_task(
        _business_spam_worker(msg.business_connection_id, msg.chat.id, owner_id, text_part, count)
    )
    spam_kb = {"inline_keyboard": [[{"text": "⛔ Остановить спам", "callback_data": "spam_stop_btn"}]]}
    await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        (
            "🔊 <b>СПАМ ЗАПУЩЕН</b>\n"
            f"<code>{LINE}</code>\n\n"
            f"◇ Отправляю: <b>{count}</b> сообщений\n"
            f"◇ Текст: <i>{html_escape(text_part[:80])}</i>\n\n"
            f"<code>{LINE}</code>\n"
            "◇ Остановить: кнопка ниже 👇\n"
            "   или команда <code>.spam stop</code>\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        ),
        reply_markup=spam_kb,
    )

@dp.callback_query(F.data == "spam_stop_btn")
async def cb_spam_stop_btn(call: CallbackQuery):
    conn_id = getattr(call, "business_connection_id", None)
    if not conn_id and call.message:
        conn_id = getattr(call.message, "business_connection_id", None)
    if not conn_id or not call.message or not call.message.chat:
        await call.answer("⛔ Не удалось остановить спам", show_alert=True)
        return
    owner_id = await _get_owner_id_cached(conn_id, "spam_stop_btn")
    if owner_id is None or call.from_user.id != owner_id:
        await call.answer("⛔ Только владелец может остановить спам", show_alert=True)
        return
    chat_id = call.message.chat.id
    key = (conn_id, chat_id, owner_id)
    task = business_spam_tasks.get(key)
    if task and not task.done():
        task.cancel()
        await call.answer("⛔ Спам остановлен", show_alert=False)
    else:
        business_spam_tasks.pop(key, None)
        await call.answer("◇ Спам не запущен", show_alert=False)
    await _business_edit_message(
        conn_id, chat_id, call.message.message_id,
        (
            "⛔ <b>СПАМ ОСТАНОВЛЕН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Рассылка прекращена\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        ),
        reply_markup={"inline_keyboard": []},
    )


# ── .mute / .unmute (Business) ─────────────────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.mute$"))
async def on_mute_inline(msg: Message):
    if not msg.business_connection_id:
        return
    if getattr(msg.chat, "type", None) != "private":
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".mute")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    business_muted_chats.add((msg.business_connection_id, msg.chat.id))
    mute_kb = {"inline_keyboard": [[{"text": "🔴 Размутить", "callback_data": "unmute_btn"}]]}
    await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        (
            "🔇 <b>MUTE ВКЛЮЧЁН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Пользователь <b>замучен</b>\n"
            "◇ Все его сообщения теперь удаляются\n\n"
            f"<code>{LINE}</code>\n"
            "◇ Размутить: кнопка ниже 👇\n"
            "   или команда <code>.unmute</code>\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        ),
        reply_markup=mute_kb,
    )

@dp.business_message(F.text.regexp(r"(?i)^\.unmute$"))
async def on_unmute_inline(msg: Message):
    if not msg.business_connection_id:
        return
    if getattr(msg.chat, "type", None) != "private":
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".unmute")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    business_muted_chats.discard((msg.business_connection_id, msg.chat.id))
    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Mute выключен")

@dp.callback_query(F.data == "unmute_btn")
async def cb_unmute_btn(call: CallbackQuery):
    conn_id = getattr(call, "business_connection_id", None)
    if not conn_id and call.message:
        conn_id = getattr(call.message, "business_connection_id", None)
    if not conn_id or not call.message or not call.message.chat:
        await call.answer("⛔ Не удалось размутить", show_alert=True)
        return
    owner_id = await _get_owner_id_cached(conn_id, "unmute_btn")
    if owner_id is None or call.from_user.id != owner_id:
        await call.answer("⛔ Только владелец может размутить", show_alert=True)
        return
    chat_id = call.message.chat.id
    key = (conn_id, chat_id)
    if key not in business_muted_chats:
        await call.answer("◇ Уже размучен", show_alert=False)
    else:
        business_muted_chats.discard(key)
        await call.answer("✅ Пользователь размучен", show_alert=False)
    await _business_edit_message(
        conn_id, chat_id, call.message.message_id,
        (
            "✅ <b>MUTE ВЫКЛЮЧЕН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Пользователь <b>размучен</b>\n"
            "◇ Сообщения снова доставляются\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        ),
        reply_markup={"inline_keyboard": []},
    )


# ── .nomute / .unnomute (Business) ─────────────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.nomute$"))
async def on_nomute_inline(msg: Message):
    if not msg.business_connection_id:
        return
    if getattr(msg.chat, "type", None) != "private":
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".nomute")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    business_nomute_chats.add((msg.business_connection_id, msg.chat.id))
    await _business_delete_message_ex(msg.business_connection_id, msg.message_id)
    nomute_kb = {"inline_keyboard": [[{"text": "🔴 Выключить nomute", "callback_data": "unnomute_btn"}]]}
    await _business_send_message_ex(
        msg.business_connection_id, msg.chat.id,
        (
            "🛡️ <b>NOMUTE ВКЛЮЧЁН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Твои сообщения дублируются ботом\n"
            "◇ Если собеседник удаляет их — копия от бота останется\n\n"
            f"<code>{LINE}</code>\n"
            "◇ Выключить: кнопка ниже 👇\n"
            "   или команда <code>.unnomute</code>\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        ),
        reply_markup=nomute_kb,
        parse_mode="HTML",
    )

@dp.business_message(F.text.regexp(r"(?i)^\.unnomute$"))
async def on_unnomute_inline(msg: Message):
    if not msg.business_connection_id:
        return
    if getattr(msg.chat, "type", None) != "private":
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".unnomute")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    business_nomute_chats.discard((msg.business_connection_id, msg.chat.id))
    await _business_delete_message_ex(msg.business_connection_id, msg.message_id)
    await _business_send_message_ex(
        msg.business_connection_id, msg.chat.id,
        (
            "🛡️ <b>NOMUTE ВЫКЛЮЧЕН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Дублирование сообщений <b>выключено</b>\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        ),
        parse_mode="HTML",
    )

@dp.callback_query(F.data == "unnomute_btn")
async def cb_unnomute_btn(call: CallbackQuery):
    conn_id = getattr(call, "business_connection_id", None)
    if not conn_id and call.message:
        conn_id = getattr(call.message, "business_connection_id", None)
    if not conn_id or not call.message or not call.message.chat:
        await call.answer("⛔ Не удалось выключить nomute", show_alert=True)
        return
    owner_id = await _get_owner_id_cached(conn_id, "unnomute_btn")
    if owner_id is None or call.from_user.id != owner_id:
        await call.answer("⛔ Только владелец может выключить nomute", show_alert=True)
        return
    chat_id = call.message.chat.id
    key = (conn_id, chat_id)
    if key not in business_nomute_chats:
        await call.answer("◇ Nomute уже выключен", show_alert=False)
    else:
        business_nomute_chats.discard(key)
        await call.answer("✅ Nomute выключен", show_alert=False)
    await _business_edit_message(
        conn_id, chat_id, call.message.message_id,
        (
            "🛡️ <b>NOMUTE ВЫКЛЮЧЕН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Дублирование сообщений <b>выключено</b>\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        ),
        reply_markup={"inline_keyboard": []},
    )


# ── .afk / .unafk (Business + ЛС с ботом) ─────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.afk(\s+.*)?$"))
async def on_afk_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".afk")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    raw_text = (msg.text or msg.caption or "").strip()
    note = raw_text[4:].strip() if len(raw_text) >= 4 else ""
    business_afk[msg.business_connection_id] = {
        "owner_id": owner_id,
        "started_at": datetime.now(timezone.utc),
        "note": note,
    }
    business_afk_last_reply.clear()
    afk_kb = {"inline_keyboard": [[{"text": "🔴 Выключить AFK", "callback_data": "unafk_btn"}]]}
    note_line = f"\n◇ <b>Заметка:</b> {html_escape(note)}" if note else ""
    await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        (
            "🌙 <b>AFK ВКЛЮЧЁН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Ты <b>не в сети</b>\n"
            "◇ Собеседникам отправляется автоответ"
            f"{note_line}\n\n"
            f"<code>{LINE}</code>\n"
            "◇ Выключить: кнопка ниже 👇\n"
            "   или команда <code>.unafk</code>\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        ),
        reply_markup=afk_kb,
    )

@dp.business_message(F.text.regexp(r"(?i)^\.unafk$"))
async def on_unafk_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".unafk")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    business_afk.pop(msg.business_connection_id, None)
    business_afk_last_reply.clear()
    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ AFK выключен")

@dp.callback_query(F.data == "unafk_btn")
async def cb_unafk_btn(call: CallbackQuery):
    conn_id = getattr(call, "business_connection_id", None)
    if not conn_id and call.message:
        conn_id = getattr(call.message, "business_connection_id", None)
    if not conn_id or not call.message or not call.message.chat:
        await call.answer("⛔ Не удалось выключить AFK", show_alert=True)
        return
    owner_id = await _get_owner_id_cached(conn_id, "unafk_btn")
    if owner_id is None or call.from_user.id != owner_id:
        await call.answer("⛔ Только владелец может выключить AFK", show_alert=True)
        return
    chat_id = call.message.chat.id
    if conn_id not in business_afk:
        await call.answer("◇ AFK уже выключен", show_alert=False)
    else:
        business_afk.pop(conn_id, None)
        business_afk_last_reply.clear()
        await call.answer("✅ AFK выключен", show_alert=False)
    await _business_edit_message(
        conn_id, chat_id, call.message.message_id,
        (
            "🌙 <b>AFK ВЫКЛЮЧЕН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Автоответ <b>выключен</b>\n"
            "◇ Снова на связи\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        ),
        reply_markup={"inline_keyboard": []},
    )

def _disable_afk(uid: int):
    """Полностью выключить AFK пользователя: глобальный и во всех бизнес-чатах."""
    user_afk.pop(uid, None)
    for conn_id in [c for c, a in business_afk.items() if a.get("owner_id") == uid]:
        business_afk.pop(conn_id, None)
    business_afk_last_reply.clear()

@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.afk(\s+.*)?$"), F.chat.type == "private")
async def on_afk_private(msg: Message):
    """Включить AFK из ЛС с ботом: автоответ во всех бизнес-чатах владельца."""
    if not msg.from_user:
        return
    uid = msg.from_user.id
    raw_text = (msg.text or msg.caption or "").strip()
    note = raw_text[4:].strip() if len(raw_text) >= 4 else ""
    user_afk[uid] = {
        "owner_id": uid,
        "started_at": datetime.now(timezone.utc),
        "note": note,
    }
    business_afk_last_reply.clear()
    note_line = f"\n◇ <b>Заметка:</b> {html_escape(note)}" if note else ""
    await msg.answer(
        f"🌙 <b>AFK включён</b>\n"
        f"<code>{LINE}</code>\n\n"
        "◇ Ты <b>не в сети</b>\n"
        "◇ Всем, кто тебе напишет в бизнес-чатах,\n"
        "   придёт автоответ"
        f"{note_line}\n\n"
        f"<code>{LINE}</code>\n"
        "◇ Выключить: кнопка ниже 👇\n"
        "   или команда <code>.unafk</code>\n\n"
        f"— 👁️ @{BOT_USERNAME}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔴 Выключить AFK", callback_data="unafk_dm")]
        ]),
    )

@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.unafk$"), F.chat.type == "private")
async def on_unafk_private(msg: Message):
    """Выключить AFK из ЛС с ботом (сбрасывает AFK и во всех бизнес-чатах)."""
    if not msg.from_user:
        return
    uid = msg.from_user.id
    _disable_afk(uid)
    await msg.answer("◇ AFK выключен")

@dp.callback_query(F.data == "unafk_dm")
async def cb_unafk_dm(call: CallbackQuery):
    """Кнопка «Выключить AFK» в ЛС с ботом (сбрасывает и бизнес-чаты)."""
    uid = call.from_user.id
    _disable_afk(uid)
    await call.answer("✅ AFK выключен", show_alert=False)
    try:
        await call.message.edit_text(
            f"🌙 <b>AFK ВЫКЛЮЧЕН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Автоответ <b>выключен</b>\n"
            "◇ Снова на связи\n\n"
            f"— 👁️ @{BOT_USERNAME}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
        )
    except Exception:
        pass


# ── .code / .uncode (Business) ─────────────────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.code$"))
async def on_code_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".code")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    business_code_mode.add(msg.business_connection_id)
    code_kb = {"inline_keyboard": [[{"text": "🔴 Выключить режим кода", "callback_data": "uncode_btn"}]]}
    await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        (
            "💻 <b>РЕЖИМ КОДА ВКЛЮЧЁН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Всё, что ты пишешь, оформляется как код\n\n"
            f"<code>{LINE}</code>\n"
            "◇ Выключить: кнопка ниже 👇\n"
            "   или команда <code>.uncode</code>\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        ),
        reply_markup=code_kb,
    )

@dp.business_message(F.text.regexp(r"(?i)^\.uncode$"))
async def on_uncode_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".uncode")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    business_code_mode.discard(msg.business_connection_id)
    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Code выключен")

@dp.callback_query(F.data == "uncode_btn")
async def cb_uncode_btn(call: CallbackQuery):
    conn_id = getattr(call, "business_connection_id", None)
    if not conn_id and call.message:
        conn_id = getattr(call.message, "business_connection_id", None)
    if not conn_id or not call.message or not call.message.chat:
        await call.answer("⛔ Не удалось выключить режим кода", show_alert=True)
        return
    owner_id = await _get_owner_id_cached(conn_id, "uncode_btn")
    if owner_id is None or call.from_user.id != owner_id:
        await call.answer("⛔ Только владелец может выключить режим кода", show_alert=True)
        return
    chat_id = call.message.chat.id
    if conn_id not in business_code_mode:
        await call.answer("◇ Режим кода уже выключен", show_alert=False)
    else:
        business_code_mode.discard(conn_id)
        await call.answer("✅ Режим кода выключен", show_alert=False)
    await _business_edit_message(
        conn_id, chat_id, call.message.message_id,
        (
            "💻 <b>РЕЖИМ КОДА ВЫКЛЮЧЕН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Обычный режим <b>возвращён</b>\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        ),
        reply_markup={"inline_keyboard": []},
    )


# ── .wbl / .unwbl (Business) ───────────────────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.wbl$"))
async def on_wbl_inline(msg: Message):
    if not msg.business_connection_id:
        return
    if getattr(msg.chat, "type", None) != "private":
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".wbl")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    business_wbl_chats.add((msg.business_connection_id, msg.chat.id))
    wbl_kb = {"inline_keyboard": [[{"text": "🔴 Выключить фильтр", "callback_data": "unwbl_btn"}]]}
    await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        (
            "🛡️ <b>ФИЛЬТР ВКЛЮЧЁН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Фильтр мата <b>включён</b>\n"
            "◇ Мат и флуд от собеседника удаляются\n\n"
            f"<code>{LINE}</code>\n"
            "◇ Выключить: кнопка ниже 👇\n"
            "   или команда <code>.unwbl</code>\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        ),
        reply_markup=wbl_kb,
    )

@dp.business_message(F.text.regexp(r"(?i)^\.unwbl$"))
async def on_unwbl_inline(msg: Message):
    if not msg.business_connection_id:
        return
    if getattr(msg.chat, "type", None) != "private":
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".unwbl")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    business_wbl_chats.discard((msg.business_connection_id, msg.chat.id))
    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Фильтр мата выключен")

@dp.callback_query(F.data == "unwbl_btn")
async def cb_unwbl_btn(call: CallbackQuery):
    conn_id = getattr(call, "business_connection_id", None)
    if not conn_id and call.message:
        conn_id = getattr(call.message, "business_connection_id", None)
    if not conn_id or not call.message or not call.message.chat:
        await call.answer("⛔ Не удалось выключить фильтр", show_alert=True)
        return
    owner_id = await _get_owner_id_cached(conn_id, "unwbl_btn")
    if owner_id is None or call.from_user.id != owner_id:
        await call.answer("⛔ Только владелец может выключить фильтр", show_alert=True)
        return
    chat_id = call.message.chat.id
    key = (conn_id, chat_id)
    if key not in business_wbl_chats:
        await call.answer("◇ Фильтр уже выключен", show_alert=False)
    else:
        business_wbl_chats.discard(key)
        await call.answer("✅ Фильтр выключен", show_alert=False)
    await _business_edit_message(
        conn_id, chat_id, call.message.message_id,
        (
            "🛡️ <b>ФИЛЬТР ВЫКЛЮЧЕН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Фильтр мата <b>выключен</b>\n"
            "◇ Сообщения снова доставляются\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        ),
        reply_markup={"inline_keyboard": []},
    )


# ── .ai (Business + группы) ────────────────────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.ai(\s+.+)?$"))
async def on_ai_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".ai")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    raw_text = msg.text or msg.caption or ""
    question = raw_text[raw_text.index(" ") + 1:].strip() if " " in raw_text else ""
    if not question:
        await _business_edit_message(
            msg.business_connection_id, msg.chat.id, msg.message_id,
            (
                "◇ <b>.ai</b> — задай вопрос — ИИ ответит.\n\n"
                "◇ <i>Как использовать:</i>\n"
                "   · <code>.ai твой вопрос</code>\n"
                "   · <code>.ai курс доллара</code>\n"
                "   · <code>.ai сделай файл</code>\n\n"
                "👁️ Модель GPT-OSS 120B"
            ),
        )
        return
    ok = await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        "◆ · · ·"
    )
    if not ok:
        return
    try:
        answer, files = await groq_chat(owner_id, question)
    except Exception as e:
        log.error(f"ai inline groq: {e}", exc_info=True)
        answer = (
            "◆ <b>ИИ недоступен</b> — вероятно, исчерпан бесплатный лимит токенов на сегодня.\n\n"
            "◇ Подожди немного и попробуй ещё раз.\n\n"
            "Quiet Mod — бесплатный бот для всех.\n"
            "Спасибо за терпение и уважение ◆"
        )
        files = []
    try:
        await _business_edit_ai_html(
            msg.business_connection_id, msg.chat.id, msg.message_id,
            prefix="", answer=f"{answer}\n\n— 👁️ @{BOT_USERNAME}"
        )
    except Exception as e:
        log.error(f"ai inline edit: {e}")
        await _business_edit_message(
            msg.business_connection_id, msg.chat.id, msg.message_id,
            f"{answer}\n\n— 👁️ @{BOT_USERNAME}"
        )
    if files:
        await _send_code_files(msg.chat.id, files, business_connection_id=msg.business_connection_id)
    log.info(f"🤖 .ai done owner={owner_id} chat={msg.chat.id}")

@dp.message(F.text.regexp(r"(?i)^\.ai(\s+.+)?$"), F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_ai_group(msg: Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    raw_text = msg.text or msg.caption or ""
    question = raw_text[raw_text.index(" ") + 1:].strip() if " " in raw_text else ""
    if not question:
        await msg.reply(
            "◇ <b>.ai</b> — задай вопрос — ИИ ответит.\n\n"
            "◇ <i>Как использовать:</i>\n"
            "   · <code>.ai твой вопрос</code>\n"
            "   · <code>.ai курс доллара</code>\n"
            "   · <code>.ai сделай файл</code>\n\n"
            "👁️ Модель GPT-OSS 120B"
        )
        return
    await db.upsert_user(uid, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    thinking = await msg.reply("◆ · · ·")
    try:
        answer, files = await groq_chat(uid, question)
    except Exception as e:
        log.error(f"ai group groq: {e}", exc_info=True)
        answer = (
            "◆ <b>ИИ недоступен</b> — вероятно, исчерпан бесплатный лимит токенов на сегодня.\n\n"
            "◇ Подожди немного и попробуй ещё раз.\n\n"
            "Quiet Mod — бесплатный бот для всех.\n"
            "Спасибо за терпение и уважение ◆"
        )
        files = []
    try:
        await _edit_ai_html(thinking, prefix="◆ ", answer=answer)
    except Exception:
        try:
            await thinking.delete()
            await _reply_ai_html(msg, prefix="◆ ", answer=answer, use_reply=True)
        except Exception as e:
            log.error(f"ai_group reply: {e}")
    if files:
        await _send_code_files(msg.chat.id, files)
    log.info(f"🤖 .ai group chat={msg.chat.id} user={uid}")


# ── .price (Business + группы) ─────────────────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.price(\s+.+)?$"))
async def on_price_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".price")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    raw_text = (msg.text or "").strip()
    body = raw_text[6:].strip() if len(raw_text) >= 6 else ""
    target = body.lstrip("@").strip() if body else ""
    if not target:
        target = (getattr(msg.chat, "username", "") or "").strip()
    if not target:
        await _business_edit_message(
            msg.business_connection_id, msg.chat.id, msg.message_id,
            "◇ <b>.price</b> — не удалось определить юзернейм.\n◇ Укажи явно: <code>.price @username</code>",
        )
        return
    estimate = _price_estimate(target)
    if estimate is None:
        await _business_edit_message(
            msg.business_connection_id, msg.chat.id, msg.message_id,
            "◇ <b>.price</b> — некорректный юзернейм. Формат: <code>.price @username</code>",
        )
        return
    await _business_edit_ai_html(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        prefix="", answer=estimate,
    )
    log.info(f"💰 .price business owner={owner_id} target=@{target}")

@dp.message(F.text.regexp(r"(?i)^\.price(\s+.+)?$"), F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_price_group(msg: Message):
    if not msg.from_user:
        return
    raw_text = msg.text or ""
    body = raw_text[6:].strip() if len(raw_text) >= 6 else ""
    target = body.lstrip("@").strip()
    if not target:
        await msg.reply("◇ <b>.price</b> — укажи юзернейм: <code>.price @username</code>")
        return
    await db.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    estimate = _price_estimate(target)
    if estimate is None:
        await msg.reply("◇ <b>.price</b> — некорректный юзернейм. Формат: <code>.price @username</code>")
        return
    await _reply_ai_html(msg, prefix="", answer=estimate)
    log.info(f"💰 .price group chat={msg.chat.id} target=@{target}")


# ── .info (Business + группы) — карточка собеседника ────────────────────
# Эвристика «ID → примерная дата регистрации». Официального API для даты
# регистрации нет, поэтому используем известные опорные точки (TelegramStats):
# ID растут со временем, и по диапазону можно грубо оценить год регистрации.
# После перехода Telegram на компактные ID (с ~2023) точность падает —
# поэтому всегда помечаем ответ как «примерно».
_INFO_ID_ANCHORS: list[tuple[int, str]] = [
    (15_000_000,   "2013"),   # первые пользователи (август 2013)
    (60_000_000,   "2014"),
    (150_000_000,  "2015"),
    (300_000_000,  "2016"),
    (500_000_000,  "2017"),
    (700_000_000,  "2018"),
    (900_000_000,  "2019"),
    (1_100_000_000, "2020"),
    (1_300_000_000, "2021"),
    (1_500_000_000, "2022"),
    (1_700_000_000, "2023"),
    (2_000_000_000, "2024"),
]


def _info_reg_year(uid: int) -> Optional[str]:
    """Грубая оценка года регистрации по ID (эвристика)."""
    if not uid or uid <= 0:
        return None
    for threshold, year in _INFO_ID_ANCHORS:
        if uid < threshold:
            return year
    return "2025+"  # очень новые аккаунты (компактные ID)


async def _info_card(uid: int, full_name: str, username: str, bio: str = "") -> str:
    """HTML-карточка собеседника для .info."""
    name = html_escape(full_name or "Неизвестно")
    uname = html_escape((username or "").lstrip("@"))
    year = _info_reg_year(uid)
    lines = [
        "ℹ️ <b>ИНФО О СОБЕСЕДНИКЕ</b>",
        f"<code>{LINE}</code>",
        "",
        f"◇ <b>Имя:</b> {name}",
    ]
    if uname:
        lines.append(f"◇ <b>Username:</b> <a href=\"https://t.me/{uname}\">@{uname}</a>")
    lines.append(f"◇ <b>ID:</b> <code>{uid}</code>")
    if bio:
        lines.append(f"◇ <b>Bio:</b> {html_escape(bio)}")
    if year:
        lines.append(
            f"◇ <b>Регистрация:</b> примерно {year}\n"
            "   <i>(оценка по ID — может отличаться)</i>"
        )
    lines += [
        "",
        f"<code>{LINE}</code>",
        f"— 👁️ @{BOT_USERNAME}",
    ]
    return "\n".join(lines)


async def _info_by_user_id(user_id: int) -> Optional[str]:
    """Собирает карточку по user_id: имя/username из getChat, bio если доступно."""
    try:
        chat = await bot.get_chat(user_id)
    except Exception as e:
        log.warning(f"ℹ️ .info getChat uid={user_id}: {e}")
        return None
    full_name = getattr(chat, "full_name", None) or getattr(chat, "title", None) or ""
    username = getattr(chat, "username", None) or ""
    bio = getattr(chat, "bio", None) or ""
    return await _info_card(user_id, full_name, username, bio)


@dp.business_message(F.text.regexp(r"(?i)^\.info(\s+.+)?$"))
async def on_info_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".info")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    raw_text = (msg.text or "").strip()
    body = raw_text[5:].strip() if len(raw_text) >= 5 else ""
    target = body.lstrip("@").strip()
    # В ЛС с ботом (business private) показываем собеседника
    if not target and msg.from_user:
        if getattr(msg.chat, "type", None) == "private" and msg.chat.id != owner_id:
            target = str(msg.chat.id)
    if not target:
        await _business_edit_message(
            msg.business_connection_id, msg.chat.id, msg.message_id,
            "◇ <b>.info</b> — показываю карточку собеседника.\n◇ Укажи явно: <code>.info @username</code> или <code>.info id</code>",
        )
        return
    if target.isdigit():
        user_id = int(target)
        card = await _info_by_user_id(user_id)
    else:
        # по username: getChat умеет резолвить @username → id
        card = await _info_by_user_id(target)
    if card is None:
        await _business_edit_message(
            msg.business_connection_id, msg.chat.id, msg.message_id,
            "◇ <b>.info</b> — не удалось получить данные.\n◇ Проверь: <code>.info @username</code> или <code>.info id</code>",
        )
        return
    await _business_edit_ai_html(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        prefix="", answer=card,
    )
    log.info(f"ℹ️ .info business owner={owner_id} target={target}")


@dp.message(F.text.regexp(r"(?i)^\.info(\s+.+)?$"), F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_info_group(msg: Message):
    if not msg.from_user:
        return
    raw_text = msg.text or ""
    body = raw_text[5:].strip() if len(raw_text) >= 5 else ""
    target = body.lstrip("@").strip()
    if not target and msg.reply_to_message and msg.reply_to_message.from_user:
        target = str(msg.reply_to_message.from_user.id)
    if not target:
        await msg.reply(
            "◇ <b>.info</b> — укажи собеседника:\n"
            "   · <code>.info @username</code>\n"
            "   · <code>.info id</code>\n"
            "   · ответь на сообщение: <code>.info</code>"
        )
        return
    await db.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    if target.isdigit():
        card = await _info_by_user_id(int(target))
    else:
        card = await _info_by_user_id(target)
    if card is None:
        await msg.reply(
            "◇ <b>.info</b> — не удалось получить данные.\n◇ Проверь: <code>.info @username</code> или <code>.info id</code>"
        )
        return
    await _reply_ai_html(msg, prefix="", answer=card)
    log.info(f"ℹ️ .info group chat={msg.chat.id} target={target}")


# ── .online / .offline (Business private + ЛС с ботом) ────────────────
# «Бесконечный онлайн»: фоновый цикл каждые ~5 сек шлёт sendChatAction(typing)
# через бизнес-подключение, поэтому собеседник видит бота (а значит и тебя)
# онлайн постоянно. Работает в ЛС с другом и в ЛС с ботом. В группах — нет.
_ONLINE_TICK_SECONDS = 5
_online_loops: dict[int, dict] = {}  # owner_id -> {"conn_id", "chat_id", "task"}


async def _online_worker(owner_id: int, conn_id: Optional[str], chat_id: int):
    """Цикл онлайн-активности: typing каждые 5 секунд, пока не выключат."""
    use_aiogram = True
    try:
        while True:
            try:
                if conn_id and use_aiogram:
                    await bot.send_chat_action(
                        chat_id, action=ChatAction.TYPING,
                        business_connection_id=conn_id,
                    )
                elif conn_id:
                    # Фолбэк на сырой API (если версия aiogram не умеет
                    # business_connection_id в sendChatAction)
                    session = get_http()
                    async with session.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendChatAction",
                        json={
                            "business_connection_id": conn_id,
                            "chat_id": chat_id,
                            "action": "typing",
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    ):
                        pass
                else:
                    await bot.send_chat_action(chat_id, action=ChatAction.TYPING)
            except TypeError as e:
                # aiogram не принял business_connection_id — переходим на сырой API
                if conn_id and use_aiogram:
                    use_aiogram = False
                    log.warning(f"💡 .online fallback to raw API (TypeError: {e})")
                    continue
                log.warning(f"💡 .online tick owner={owner_id}: {e}")
                await asyncio.sleep(_ONLINE_TICK_SECONDS * 2)
                continue
            except TelegramRetryAfter as e:
                await asyncio.sleep(int(e.retry_after))
                continue
            except Exception as e:
                # Подключение умерло или Telegram не принял действие —
                # пробуем ещё пару раз, потом отключаемся автоматически.
                log.warning(f"💡 .online tick owner={owner_id}: {e}")
                await asyncio.sleep(_ONLINE_TICK_SECONDS * 2)
                continue
            await asyncio.sleep(_ONLINE_TICK_SECONDS)
    except asyncio.CancelledError:
        pass
    finally:
        _online_loops.pop(owner_id, None)


async def _online_first_conn_id() -> Optional[str]:
    """ID первого бизнес-подключения владельца (сырой API — работает на любой версии)."""
    try:
        session = get_http()
        async with session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getMyBusinessConnections",
            json={},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
        if not data.get("ok"):
            return None
        conns = (data.get("result") or {}).get("connections") or []
        if not conns:
            return None
        return conns[0].get("id")
    except Exception as e:
        log.warning(f"💡 .online getMyBusinessConnections: {e}")
        return None


def _online_stop(owner_id: int):
    """Выключает онлайн-цикл пользователя (если запущен)."""
    entry = _online_loops.pop(owner_id, None)
    if entry:
        task = entry.get("task")
        if task and not task.done():
            task.cancel()


async def _online_start(owner_id: int, conn_id: Optional[str], chat_id: int):
    """Запускает онлайн-цикл (перезапуск без дублей)."""
    _online_stop(owner_id)
    task = asyncio.create_task(_online_worker(owner_id, conn_id, chat_id))
    _online_loops[owner_id] = {"conn_id": conn_id, "chat_id": chat_id, "task": task}


async def _online_send_status(conn_id: Optional[str], chat_id: int, msg_id: int, running: bool, owner_id: int):
    """Сообщение о состоянии онлайн-режима + кнопка «Выключить»."""
    if running:
        text = (
            "💡 <b>ОНЛАЙН ВКЛЮЧЁН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Бот поддерживает онлайн постоянно\n"
            "◇ Собеседник видит тебя в сети\n\n"
            f"<code>{LINE}</code>\n"
            "◇ Выключить: кнопка ниже 👇\n"
            "   или команда <code>.offline</code>\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        )
        kbd = {"inline_keyboard": [[{"text": "🔴 Выключить онлайн", "callback_data": "online_off_btn"}]]}
    else:
        text = "◇ Онлайн выключен"
        kbd = {"inline_keyboard": []}
    if conn_id:
        await _business_edit_message(conn_id, chat_id, msg_id, text, reply_markup=kbd)
    else:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=kbd)
        except Exception:
            pass


@dp.business_message(F.text.regexp(r"(?i)^\.online$"))
async def on_online_inline(msg: Message):
    if not msg.business_connection_id:
        return
    if getattr(msg.chat, "type", None) != "private":
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".online")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    await _online_start(owner_id, msg.business_connection_id, msg.chat.id)
    await _online_send_status(msg.business_connection_id, msg.chat.id, msg.message_id, True, owner_id)
    log.info(f"💡 .online business owner={owner_id} chat={msg.chat.id}")


@dp.business_message(F.text.regexp(r"(?i)^\.offline$"))
async def on_offline_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".offline")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    _online_stop(owner_id)
    await _online_send_status(msg.business_connection_id, msg.chat.id, msg.message_id, False, owner_id)
    log.info(f"💡 .offline business owner={owner_id}")


@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.online$"), F.chat.type == "private")
async def on_online_private(msg: Message):
    """ЛС с ботом: запускаем онлайн через доступное бизнес-подключение."""
    if not msg.from_user:
        return
    uid = msg.from_user.id
    conn_id = await _online_first_conn_id()
    if not conn_id:
        await msg.answer(
            "◇ <b>.online</b> — не найдено бизнес-подключение.\n"
            "◇ Напиши команду в ЛС с другом, чтобы включить онлайн там."
        )
        return
    await _online_start(uid, conn_id, msg.chat.id)
    await _online_send_status(None, msg.chat.id, msg.message_id, True, uid)
    log.info(f"💡 .online dm user={uid} conn={conn_id}")


@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.offline$"), F.chat.type == "private")
async def on_offline_private(msg: Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    _online_stop(uid)
    await _online_send_status(None, msg.chat.id, msg.message_id, False, uid)
    log.info(f"💡 .offline dm user={uid}")


@dp.callback_query(F.data == "online_off_btn")
async def cb_online_off_btn(call: CallbackQuery):
    """Кнопка «Выключить онлайн» в бизнес-чате."""
    conn_id = getattr(call, "business_connection_id", None)
    if not conn_id and call.message:
        conn_id = getattr(call.message, "business_connection_id", None)
    if not conn_id or not call.message or not call.message.chat:
        await call.answer("⛔ Не удалось выключить онлайн", show_alert=True)
        return
    owner_id = await _get_owner_id_cached(conn_id, "online_off_btn")
    if owner_id is None or call.from_user.id != owner_id:
        await call.answer("⛔ Только владелец может выключить онлайн", show_alert=True)
        return
    _online_stop(owner_id)
    await call.answer("💡 Онлайн выключен", show_alert=False)
    await _business_edit_message(
        conn_id, call.message.chat.id, call.message.message_id,
        (
            "💡 <b>ОНЛАЙН ВЫКЛЮЧЕН</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Поддержка онлайна <b>остановлена</b>\n\n"
            f"— 👁️ @{BOT_USERNAME}"
        ),
        reply_markup={"inline_keyboard": []},
    )


# ── .curs (Business + группы/ЛС) ───────────────────────────────────────
_CURS_KBD = {
    "inline_keyboard": [
        [{"text": "🧮 Калькулятор", "callback_data": "curs_calc"}],
        [{"text": "✕ Закрыть", "callback_data": "curs_close"}],
    ]
}


def _curs_kb_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧮 Калькулятор", callback_data="curs_calc")],
        [InlineKeyboardButton(text="✕ Закрыть", callback_data="curs_close")],
    ])


def _curs_edit_kb_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧮 Калькулятор", callback_data="curs_calc")],
        [InlineKeyboardButton(text="← К курсу", callback_data="curs_back")],
        [InlineKeyboardButton(text="✕ Закрыть", callback_data="curs_close")],
    ])


def _curs_prompt() -> str:
    return (
        "🧮 <b>Калькулятор валют</b>\n"
        f"<code>{LINE}</code>\n\n"
        "Введи просто число в рублях —\n"
        "покажу, сколько это в разных валютах.\n\n"
        "◇ Пример: <code>100</code> · <code>100₽</code> · <code>1 500</code> · <code>1250,50</code>"
    )


def _parse_curs_amount(text: str) -> Optional[float]:
    """Сумма в рублях: 100 · 100₽ · 100 руб · 100р · 1 500 · 1250,50. None — некорректно."""
    t = (text or "").strip()
    t = re.sub(r"(?iu)\s*(₽|рублей|рубля|рубл|руб\.|руб|р\.|р(?![а-яa-z])|rub|rur)\s*$", "", t).strip()
    t = t.replace("\u202f", "").replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", t):
        return None
    try:
        v = float(t)
    except Exception:
        return None
    if v <= 0 or v > 10**12:
        return None
    return v


def _render_curs_calc(amount: float, rates: dict[str, float]) -> str:
    lines = []
    for code, flag, name in _POPULAR_CURS:
        rub = rates.get(code)
        if rub is None or rub <= 0:
            continue
        value = amount / rub
        lines.append(f"{flag} {name:<12} → <code>{_fmt_curs_rate(value):>10}</code>")
    if not lines:
        return "⚠️ <b>Не удалось получить курсы</b> — попробуй позже."
    return (
        f"🧮 <b>{_fmt_curs_rate(amount)} ₽</b> — в других валютах\n"
        f"<code>{LINE}</code>\n\n"
        + "\n".join(lines)
        + f"\n\n<code>{LINE}</code>\n"
        f"◇ Курс: официальный ЦБ РФ · {date.today().strftime('%d.%m.%Y')}\n"
        f"— 👁️ @{BOT_USERNAME}"
    )


def _curs_call_context(call: CallbackQuery) -> tuple[Optional[str], int, int]:
    conn_id = getattr(call, "business_connection_id", None)
    if not conn_id and call.message:
        conn_id = getattr(call.message, "business_connection_id", None)
    if not call.message or not call.message.chat:
        return None, 0, 0
    return conn_id, call.message.chat.id, call.message.message_id


async def _curs_edit_message(conn_id: Optional[str], chat_id: int, msg_id: int, text: str, reply_markup) -> bool:
    if conn_id:
        markup = reply_markup.model_dump(exclude_none=True) if isinstance(reply_markup, InlineKeyboardMarkup) else reply_markup
        return await _business_edit_message(conn_id, chat_id, msg_id, text, reply_markup=markup)
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=reply_markup)
        return True
    except Exception:
        return False


async def _curs_delete_message(conn_id: Optional[str], chat_id: int, msg_id: int):
    if conn_id:
        try:
            await _business_delete_message_ex(conn_id, msg_id)
        except Exception:
            pass
    else:
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass


# ── Калькулятор .curs: сессии без FSM (надёжно работает и в бизнес-чатах) ──
_CURS_SESSION_TTL = 10 * 60  # сессия калькулятора живёт 10 минут
_curs_sessions: dict[tuple, dict] = {}  # (chat_id, user_id) -> {"msg_id", "ts"}


def _curs_session_key(conn_id: Optional[str], chat_id: int, user_id: int) -> tuple:
    # Ключ — чат + юзер: чат принадлежит одному подключению, поэтому
    # business_connection_id в ключе не нужен (исключаем расхождение между
    # callback_query и business_message в бизнес-чатах).
    return (chat_id, user_id)


def _curs_session_save(conn_id: Optional[str], chat_id: int, user_id: int, msg_id: int):
    # time.monotonic() вместо asyncio.get_running_loop().time():
    # сессию читают фильтры хендлеров, а их aiogram запускает в отдельном
    # потоке (run_in_executor), где нет running event loop.
    now = time.monotonic()
    for k in [k for k, v in _curs_sessions.items() if now - v.get("ts", 0) > _CURS_SESSION_TTL]:
        _curs_sessions.pop(k, None)
    _curs_sessions[_curs_session_key(conn_id, chat_id, user_id)] = {"msg_id": msg_id, "ts": now}


def _curs_session_get(conn_id: Optional[str], chat_id: int, user_id: int) -> Optional[dict]:
    key = _curs_session_key(conn_id, chat_id, user_id)
    data = _curs_sessions.get(key)
    if not data:
        return None
    now = time.monotonic()
    if now - data.get("ts", 0) > _CURS_SESSION_TTL:
        _curs_sessions.pop(key, None)
        return None
    return data


@dp.business_message(F.text.regexp(r"(?i)^\.curs$"))
async def on_curs_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".curs")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    curs_text, _ = await _get_curs_cached()
    if curs_text is None:
        curs_text = "⚠️ <b>Не удалось получить курс</b> — сервис временно недоступен. Попробуй позже."
    ok, retry_after, _ = await _business_delete_message_ex(msg.business_connection_id, msg.message_id)
    if not ok and retry_after:
        await asyncio.sleep(int(retry_after))
        await _business_delete_message_ex(msg.business_connection_id, msg.message_id)
    ok, retry_after, _ = await _business_send_message_ex(
        msg.business_connection_id, msg.chat.id, curs_text,
        reply_markup=_CURS_KBD, parse_mode="HTML",
    )
    if not ok and retry_after:
        await asyncio.sleep(int(retry_after))
        await _business_send_message_ex(
            msg.business_connection_id, msg.chat.id, curs_text,
            reply_markup=_CURS_KBD, parse_mode="HTML",
        )
    log.info(f"💱 .curs business owner={owner_id} chat={msg.chat.id}")


@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.curs$"), F.chat.type.in_({"private", "group", "supergroup", "channel"}))
async def on_curs_anywhere(msg: Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    await db.upsert_user(uid, msg.from_user.username or "", msg.from_user.full_name or "")
    if msg.chat.type in ("group", "supergroup", "channel"):
        await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    curs_text, _ = await _get_curs_cached()
    if curs_text is None:
        curs_text = "⚠️ <b>Не удалось получить курс</b> — сервис временно недоступен. Попробуй позже."
    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass
    await msg.answer(curs_text, reply_markup=_curs_kb_markup())
    log.info(f"💱 .curs chat={msg.chat.id} user={uid}")


@dp.callback_query(F.data == "curs_close")
async def cb_curs_close(call: CallbackQuery):
    """Кнопка «Закрыть» — удаляет сообщение с курсом/калькулятором."""
    conn_id, chat_id, msg_id = _curs_call_context(call)
    _curs_sessions.pop(_curs_session_key(conn_id, chat_id, call.from_user.id), None)
    if not msg_id:
        await call.answer("✕ Закрыто", show_alert=False)
        return
    await _curs_delete_message(conn_id, chat_id, msg_id)
    await call.answer("✕ Закрыто", show_alert=False)


@dp.callback_query(F.data == "curs_back")
async def cb_curs_back(call: CallbackQuery):
    """«← К курсу» — возвращает таблицу курсов (берём из кэша, без стейта)."""
    conn_id, chat_id, msg_id = _curs_call_context(call)
    _curs_sessions.pop(_curs_session_key(conn_id, chat_id, call.from_user.id), None)
    if not msg_id:
        await call.answer("←", show_alert=False)
        return
    text, _ = await _get_curs_cached()
    if text is None:
        text = "⚠️ Курс недоступен — набери <code>.curs</code> заново."
    await _curs_edit_message(conn_id, chat_id, msg_id, text, _curs_kb_markup())
    await call.answer()


@dp.callback_query(F.data == "curs_calc")
async def cb_curs_calc(call: CallbackQuery):
    """«Калькулятор» — просим ввести сумму в рублях."""
    conn_id, chat_id, msg_id = _curs_call_context(call)
    if not msg_id:
        await call.answer("⛔", show_alert=True)
        return
    _curs_session_save(conn_id, chat_id, call.from_user.id, msg_id)
    await _curs_edit_message(conn_id, chat_id, msg_id, _curs_prompt(), InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← К курсу", callback_data="curs_back")],
        [InlineKeyboardButton(text="✕ Закрыть", callback_data="curs_close")],
    ]))
    await call.answer()


def _curs_calc_active_business(msg: Message) -> bool:
    return bool(msg.business_connection_id and msg.from_user) and _curs_session_get(
        msg.business_connection_id, msg.chat.id, msg.from_user.id
    ) is not None


def _curs_calc_active_regular(msg: Message) -> bool:
    return bool(msg.from_user) and _curs_session_get(
        None, msg.chat.id, msg.from_user.id
    ) is not None


async def _curs_handle_amount(conn_id: Optional[str], chat_id: int, user_id: int, text: str, delete_fn, reply_fn):
    """Общая логика калькулятора: сумма → конвертация в то же сообщение."""
    session = _curs_session_get(conn_id, chat_id, user_id)
    if not session:
        return
    amount = _parse_curs_amount(text)
    if amount is None:
        if re.search(r"\d", text or ""):
            await reply_fn("◇ Введи число в рублях, например: <code>300</code>")
        return
    await delete_fn()
    _, rates = await _get_curs_cached()
    result = _render_curs_calc(amount, rates or {})
    ok = await _curs_edit_message(conn_id, chat_id, session["msg_id"], result, _curs_edit_kb_markup())
    if not ok:
        await reply_fn(result)
    _curs_sessions.pop(_curs_session_key(conn_id, chat_id, user_id), None)


@dp.business_message(_curs_calc_active_business)
async def on_curs_amount_business(msg: Message):
    """Сумма в рублях из бизнес-чата (ЛС с другом и т.п.)."""
    async def _delete():
        try:
            await _business_delete_message_ex(msg.business_connection_id, msg.message_id)
        except Exception:
            pass

    async def _reply(text: str):
        try:
            await _business_send_message_ex(msg.business_connection_id, msg.chat.id, text, parse_mode="HTML")
        except Exception:
            pass

    await _curs_handle_amount(
        msg.business_connection_id, msg.chat.id, msg.from_user.id,
        msg.text or "", _delete, _reply,
    )


@dp.message(_curs_calc_active_regular)
async def on_curs_amount(msg: Message):
    """Сумма в рублях из ЛС с ботом или группы."""
    async def _delete():
        try:
            await bot.delete_message(msg.chat.id, msg.message_id)
        except Exception:
            pass

    async def _reply(text: str):
        await msg.answer(text)

    await _curs_handle_amount(
        None, msg.chat.id, msg.from_user.id,
        msg.text or "", _delete, _reply,
    )


# ── Форматирование: .bold .italic .mono .line .crossed .hidden .quote ─
FORMAT_CMDS: dict[str, tuple[str, str]] = {
    "bold":    ("<b>", "</b>"),
    "italic":  ("<i>", "</i>"),
    "mono":    ("<code>", "</code>"),
    "line":    ("<u>", "</u>"),
    "crossed": ("<s>", "</s>"),
    "hidden":  ("<tg-spoiler>", "</tg-spoiler>"),
    "quote":   ("<blockquote>", "</blockquote>"),
}
FORMAT_CMD_RE = r"(?is)^\.(bold|italic|mono|line|crossed|hidden|quote)\s+(.+)$"

def _format_inline_cmd(text: str) -> Optional[tuple[str, str]]:
    m = re.match(FORMAT_CMD_RE, text or "")
    if not m:
        return None
    cmd = m.group(1).lower()
    content = m.group(2).strip()
    if not content:
        return None
    open_tag, close_tag = FORMAT_CMDS[cmd]
    return cmd, f"{open_tag}{html_escape(content)}{close_tag}"

@dp.business_message(F.text.regexp(FORMAT_CMD_RE))
async def on_format_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, "format")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    parsed = _format_inline_cmd(msg.text or msg.caption or "")
    if not parsed:
        return
    cmd, formatted = parsed
    ok = await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id, formatted
    )
    log.info(f"✏️ .{cmd} business chat={msg.chat.id} owner={owner_id} ok={ok}")

@dp.message(F.text.regexp(FORMAT_CMD_RE), F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_format_group(msg: Message):
    if not msg.from_user:
        return
    parsed = _format_inline_cmd(msg.text or msg.caption or "")
    if not parsed:
        return
    cmd, formatted = parsed
    try:
        await msg.delete()
    except Exception:
        pass
    await msg.answer(formatted)
    log.info(f"✏️ .{cmd} group chat={msg.chat.id} user={msg.from_user.id}")


# ── .spam (в любом чате) ───────────────────────────────────────────────
async def _spam_worker(chat_id: int, uid: int, text: str, count: int):
    key = (chat_id, uid)
    try:
        for _ in range(count):
            try:
                await bot.send_message(chat_id, text, parse_mode=None)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                await bot.send_message(chat_id, text, parse_mode=None)
            await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error(f"spam worker: {e}")
    finally:
        spam_tasks.pop(key, None)

@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.spam(\s+.+)?$"), F.chat.type.in_({"private", "group", "supergroup", "channel"}))
async def on_spam(msg: Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    raw_text = (msg.text or msg.caption or "").strip()
    body = raw_text[5:].strip() if len(raw_text) >= 5 else ""
    key = (msg.chat.id, uid)
    if body.lower() == "stop":
        task = spam_tasks.get(key)
        if task and not task.done():
            task.cancel()
            await msg.answer("◇ Спам остановлен", parse_mode=None)
        else:
            spam_tasks.pop(key, None)
            await msg.answer("◇ Спам не запущен", parse_mode=None)
        return
    if not body or " " not in body:
        await msg.answer("◇ Формат: .spam текст 10  |  .spam stop", parse_mode=None)
        return
    text_part, count_part = body.rsplit(" ", 1)
    try:
        count = int(count_part)
    except Exception:
        await msg.answer("◇ Количество должно быть числом: .spam текст 10", parse_mode=None)
        return
    if count <= 0:
        await msg.answer("◇ Количество должно быть > 0", parse_mode=None)
        return
    existing = spam_tasks.get(key)
    if existing and not existing.done():
        await msg.answer("◇ Спам уже идёт. Остановить: .spam stop", parse_mode=None)
        return
    spam_tasks[key] = asyncio.create_task(_spam_worker(msg.chat.id, uid, text_part, count))
    await msg.answer(f"◇ Запустил спам: {count}", parse_mode=None)
