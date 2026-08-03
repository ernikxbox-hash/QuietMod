"""Все точечные команды: .spam .mute .nomute .afk .code .wbl .ai .price .curs и форматирование."""
import asyncio
import re
from datetime import datetime, timezone
from typing import Optional

from aiogram import F
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
from core import BOT_USERNAME, bot, dp, log
from functions import (
    LINE,
    _business_edit_ai_html,
    _edit_ai_html,
    _get_curs_ru,
    _get_image_base64,
    _price_estimate,
    _reply_ai_html,
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
@dp.business_message(F.text.regexp(r"(?i)^\.ai\s+.+"))
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
    ok = await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        "◆ · · ·"
    )
    if not ok:
        return
    image_b64 = None
    if msg.photo:
        image_b64 = await _get_image_base64(bot, msg.photo[-1].file_id)
    try:
        answer = await groq_chat(owner_id, question or "Опиши что на фото.", image_base64=image_b64)
    except Exception as e:
        log.error(f"ai inline groq: {e}", exc_info=True)
        answer = (
            "◆ <b>ИИ недоступен</b> — вероятно, исчерпан бесплатный лимит токенов на сегодня.\n\n"
            "◇ Подожди немного и попробуй ещё раз.\n\n"
            "Quiet Mod — бесплатный бот для всех.\n"
            "Спасибо за терпение и уважение ◆"
        )
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
    log.info(f"🤖 .ai done owner={owner_id} chat={msg.chat.id} with_photo={image_b64 is not None}")

@dp.message(F.text.regexp(r"(?i)^\.ai\s+.+"), F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_ai_group(msg: Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    raw_text = msg.text or msg.caption or ""
    question = raw_text[raw_text.index(" ") + 1:].strip() if " " in raw_text else ""
    if not question:
        return
    await db.upsert_user(uid, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    thinking = await msg.reply("◆ · · ·")
    image_b64 = None
    if msg.photo:
        image_b64 = await _get_image_base64(bot, msg.photo[-1].file_id)
    try:
        answer = await groq_chat(uid, question, image_base64=image_b64)
    except Exception as e:
        log.error(f"ai group groq: {e}", exc_info=True)
        answer = (
            "◆ <b>ИИ недоступен</b> — вероятно, исчерпан бесплатный лимит токенов на сегодня.\n\n"
            "◇ Подожди немного и попробуй ещё раз.\n\n"
            "Quiet Mod — бесплатный бот для всех.\n"
            "Спасибо за терпение и уважение ◆"
        )
    try:
        await _edit_ai_html(thinking, prefix="◆ ", answer=answer)
    except Exception:
        try:
            await thinking.delete()
            await _reply_ai_html(msg, prefix="◆ ", answer=answer, use_reply=True)
        except Exception as e:
            log.error(f"ai_group reply: {e}")
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


# ── .curs (Business + группы/ЛС) ───────────────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.curs$"))
async def on_curs_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".curs")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    curs_text = await _get_curs_ru()
    if curs_text is None:
        curs_text = "⚠️ <b>Не удалось получить курс</b> — сервис временно недоступен. Попробуй позже."
    await _business_edit_ai_html(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        prefix="", answer=curs_text,
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
    curs_text = await _get_curs_ru()
    if curs_text is None:
        curs_text = "⚠️ <b>Не удалось получить курс</b> — сервис временно недоступен. Попробуй позже."
    await _reply_ai_html(msg, prefix="", answer=curs_text)
    log.info(f"💱 .curs chat={msg.chat.id} user={uid}")


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
