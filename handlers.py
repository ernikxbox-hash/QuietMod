import asyncio
import logging
import os
import random
import re
import signal
from datetime import datetime, timedelta, timezone
from typing import Optional
import aiohttp
from ddgs import DDGS
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BusinessMessagesDeleted,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from html import escape as html_escape
import database as db
from core import (
    BOT_TOKEN,
    ADMIN_ID,
    BRAND_NAME,
    GROQ_API_KEYS,
    S,
    bot,
    dp,
    log,
)
from functions import *
from functions import (
    _business_edit_ai_html,
    _contains_profanity,
    _wbl_should_delete,
    _ddg_search,
    _edit_ai_html,
    _extract_city,
    _get_image_base64,
    _get_weather,
    _groq_request,
    _is_weather_query,
    _normalize_code_blocks,
    _price_estimate,
    _reply_ai_html,
    _send_notify,
    _show_home,
)
from business_api import (
    _business_delete_message_ex,
    _business_edit_message,
    _business_edit_message_ex,
    _business_send_message_ex,
)
_BC_OWNER_CACHE: dict[str, tuple[int, float]] = {}
_BC_OWNER_TTL_SECONDS = 10 * 60
async def _get_owner_id_cached(conn_id: str, ctx: str) -> Optional[int]:
    now = asyncio.get_running_loop().time()
    cached = _BC_OWNER_CACHE.get(conn_id)
    if cached and cached[1] > now:
        return cached[0]
    try:
        conn = await bot.get_business_connection(conn_id)
        owner_id = conn.user.id
    except Exception as e:
        log.error(f"get_business_connection ({ctx}): {e}")
        return None
    _BC_OWNER_CACHE[conn_id] = (owner_id, now + _BC_OWNER_TTL_SECONDS)
    return owner_id
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    uid   = msg.from_user.id
    name  = msg.from_user.full_name or "—"
    uname = msg.from_user.username or ""
    referrer_id: Optional[int] = None
    parts = msg.text.split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            rid = int(parts[1][4:])
            if rid != uid:
                referrer_id = rid
        except ValueError:
            pass
    existing = await db.get_user(uid)
    await db.upsert_user(uid, uname, name, referrer_id if not existing else None)
    if not existing and referrer_id:
        try:
            await bot.send_message(
                referrer_id,
                f"◆ <b>Новый реферал</b>\n{LINE}\n"
                f"<b>{name}</b> присоединился по твоей ссылке.",
            )
        except Exception:
            pass
    home_text_full = (
        f"◆ <b>QUIET MOD</b> 👁️\n"
        f"<code>{LINE}</code>\n\n"
        f"<b>{html_escape(name)}</b>, добро пожаловать в тишину.\n\n"
        "Я слежу за тем, что исчезает —\n"
        "<b>удалённые и изменённые</b> сообщения\n"
        "появятся здесь раньше, чем их забудут.\n\n"
        f"<code>{LINE}</code>\n"
        f"◇ Статус       <b>Свободен · без лимитов</b>\n"
        f"◇ Перехват     <b>безлимит</b>\n"
        f"◇ Архив        <b>безлимит</b>\n"
        f"◇ Поиск        <b>включён</b>\n"
        f"◇ ИИ           <b>без лимитов</b>\n"
        f"<code>{LINE}</code>\n\n"
        f"◇ Пригласить:\n"
        f"<code>{ref_link(uid)}</code>"
    )
    await _show_home(uid, home_text_full, kb_main(uid), msg)
@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(
        f"▲ <b>Admin Suite</b>\n{LINE}",
        reply_markup=kb_admin(),
    )
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
    nomute_kb = {"inline_keyboard": [[{"text": "🔴 Выключить nomute", "callback_data": "unnomute_btn"}]]}
    await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
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
    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Nomute выключен")
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
@dp.business_message(F.text.regexp(r"(?i)^\.search\s+.+"))
async def on_search_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".search")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    raw_text = msg.text or ""
    query = raw_text[raw_text.index(" ") + 1:].strip() if " " in raw_text else ""
    if not query:
        return
    ok = await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◐ ·")
    if not ok:
        return
    await asyncio.sleep(1)
    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◐ · ·")
    await asyncio.sleep(1)
    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◐ · · ·")
    await asyncio.sleep(1)
    if _is_weather_query(query):
        city = _extract_city(query)
        weather_text = await _get_weather(city) if city else None
        if weather_text:
            answer = weather_text
        elif city:
            answer = f"⚠️ Не нашёл город «{city}» — проверь название и попробуй ещё раз."
        else:
            answer = "🌤 Уточни город, например: .search погода в Москве"
    else:
        search_results = await _ddg_search(query)
        if search_results:
            prompt = (
                f"Пользователь ищет: «{query}»\n\n"
                f"Результаты поиска:\n{search_results}\n\n"
                "Дай чёткий и актуальный ответ на основе этих данных. Кратко, по делу."
            )
        else:
            prompt = f"Найди и расскажи всё что знаешь про: {query}"
        answer = await _groq_request([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ], model=GROQ_MODEL_TEXT)
        if not answer:
            answer = "⚠️ Не удалось получить результаты поиска — попробуй позже."
        else:
            answer = _normalize_code_blocks(answer)
    await _business_edit_ai_html(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        prefix="◐ ", answer=f"{answer}\n\n— 👁️ @{BOT_USERNAME}"
    )
    log.info(f"🔍 .search done owner={owner_id} query={query[:50]}")
@dp.message(F.text.regexp(r"(?i)^\.search\s+.+"), F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_search_group(msg: Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    raw_text = msg.text or ""
    query = raw_text[raw_text.index(" ") + 1:].strip() if " " in raw_text else ""
    if not query:
        return
    await db.upsert_user(uid, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    thinking = await msg.reply("◐ · · ·")
    if _is_weather_query(query):
        city = _extract_city(query)
        weather_text = await _get_weather(city) if city else None
        if weather_text:
            answer = weather_text
        elif city:
            answer = f"⚠️ Не нашёл город «{city}» — проверь название и попробуй ещё раз."
        else:
            answer = "🌤 Уточни город, например: .search погода в Москве"
    else:
        search_results = await _ddg_search(query)
        if search_results:
            prompt = (
                f"Пользователь ищет: «{query}»\n\n"
                f"Результаты поиска:\n{search_results}\n\n"
                "Дай чёткий и актуальный ответ на основе этих данных. Кратко, по делу."
            )
        else:
            prompt = f"Найди и расскажи всё что знаешь про: {query}"
        answer = await _groq_request([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ], model=GROQ_MODEL_TEXT)
        if not answer:
            answer = "⚠️ Не удалось получить результаты поиска — попробуй позже."
        else:
            answer = _normalize_code_blocks(answer)
    try:
        await _edit_ai_html(thinking, prefix="◐ ", answer=answer)
    except Exception:
        try:
            await thinking.delete()
            await _reply_ai_html(msg, prefix="◐ ", answer=answer, use_reply=True)
        except Exception as e:
            log.error(f"search_group reply: {e}")
    log.info(f"🔍 .search group chat={msg.chat.id} user={uid} query={query[:50]}")
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
# ── ⚔️ КАМЕНЬ · НОЖНИЦЫ · БУМАГА (.knb) ────────────────────────────
_KPB_EMOJI = {"r": "✊", "s": "✌️", "p": "🖐"}
_KPB_BEATS = {"r": "s", "s": "p", "p": "r"}
_KNB_STALE_SECONDS = 15 * 60

_GROUP_MEMBERS: dict[int, dict[int, dict]] = {}  # chat_id -> {user_id: {id, username, full_name}}

def _knb_game_alive(game: dict) -> bool:
    """True, если игра/вызов ещё актуальна (не протухла за 15 минут)."""
    return (asyncio.get_running_loop().time() - game.get("ts", 0)) < _KNB_STALE_SECONDS

def _knb_cache_member(chat_id: int, user) -> None:
    if not user or not getattr(user, "id", None):
        return
    members = _GROUP_MEMBERS.setdefault(chat_id, {})
    members[user.id] = {
        "id": user.id,
        "username": user.username or "",
        "full_name": user.full_name or "",
    }

def _knb_display_name(name: str, username: str) -> str:
    if username:
        return f"@{username}"
    return html_escape(name) or "Собеседник"

def _knb_header(game: dict) -> str:
    head = (
        f"⚔️ <b>КАМЕНЬ · НОЖНИЦЫ · БУМАГА</b>\n"
        f"<code>{LINE}</code>\n\n"
        f"◇ {game['a_name']}  <b>vs</b>  {game['b_name']}\n\n"
    )
    sa = game.get("score_a", 0)
    sb = game.get("score_b", 0)
    if sa or sb:
        head += (
            f"📊 Счёт: {game['a_name']}  <b>{sa}:{sb}</b>  {game['b_name']}\n\n"
        )
    return head

def _knb_challenge_text(game: dict) -> str:
    a_link = f"<a href=\"tg://user?id={game['a_id']}\">{game['a_name']}</a>"
    b_link = f"<a href=\"tg://user?id={game['b_id']}\">{game['b_name']}</a>"
    return (
        "⚔️ <b>ВЫЗОВ НА БОЙ!</b>\n"
        f"<code>{LINE}</code>\n\n"
        f"◇ {a_link}  бросил(а) вызов  {b_link}\n"
        "◇ Камень · Ножницы · Бумага — 1 на 1\n\n"
        f"<code>{LINE}</code>\n"
        f"◇ {b_link}, жми <b>«⚔️ Принять бой»</b> 👇"
    )

def _knb_move_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✊", callback_data="knb_r"),
            InlineKeyboardButton(text="✌️", callback_data="knb_s"),
            InlineKeyboardButton(text="🖐", callback_data="knb_p"),
        ],
        [InlineKeyboardButton(text="✕ Отменить", callback_data="knb_cancel")],
    ])

def _knb_result_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Сыграть ещё", callback_data="knb_again")],
        [InlineKeyboardButton(text="✕ Закрыть", callback_data="knb_cancel")],
    ])

def _knb_challenge_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ Принять бой", callback_data="knb_accept")],
        [InlineKeyboardButton(text="✕ Отменить", callback_data="knb_cancel")],
    ])

async def _knb_edit(game: dict, msg_id: int, text: str, reply_markup=None) -> bool:
    if game.get("conn_id"):
        markup = reply_markup.model_dump(exclude_none=True) if isinstance(reply_markup, InlineKeyboardMarkup) else reply_markup
        return await _business_edit_message(game["conn_id"], game["chat_id"], msg_id, text, reply_markup=markup)
    try:
        await bot.edit_message_text(
            text,
            chat_id=game["chat_id"],
            message_id=msg_id,
            reply_markup=reply_markup,
        )
        return True
    except Exception as e:
        log.warning(f"knb edit: {e}")
        return False

def _knb_game_key_from_call(call: CallbackQuery) -> Optional[tuple]:
    if not call.message or not call.message.chat:
        return None
    conn_id = getattr(call, "business_connection_id", None)
    if not conn_id and call.message:
        conn_id = getattr(call.message, "business_connection_id", None)
    chat_id = call.message.chat.id
    if conn_id:
        prefix = "dm" if getattr(call.message.chat, "type", None) == "private" else "bg"
        return (prefix, conn_id, chat_id)
    return ("group", chat_id)

async def _knb_resolve_target(msg: Message) -> Optional[dict]:
    """Определяет, кого вызывают на бой: mention-сущность, @username из кэша или reply."""
    for ent in (msg.entities or []):
        if ent.type == "text_mention" and ent.user:
            u = ent.user
            return {"id": u.id, "full_name": u.full_name or "", "username": u.username or ""}
    m = re.match(r"(?i)^\.knb\s+@?([a-zA-Z0-9_]{3,32})$", (msg.text or "").strip())
    if m:
        uname = m.group(1).lower()
        for info in _GROUP_MEMBERS.get(msg.chat.id, {}).values():
            if (info.get("username") or "").lower() == uname:
                return dict(info)
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return {"id": u.id, "full_name": u.full_name or "", "username": u.username or ""}
    return None

@dp.business_message(F.text.regexp(r"(?i)^\.knb(\s+.*)?$"))
async def on_knb_start(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return
    owner_id = await _get_owner_id_cached(conn_id, ".knb")
    if owner_id is None:
        return
    chat_type = getattr(msg.chat, "type", None)
    if chat_type == "private":
        # ── ЛС (Business): мгновенная игра владелец vs собеседник ──
        if not msg.from_user or msg.from_user.id != owner_id:
            return
        key = ("dm", conn_id, msg.chat.id)
        if key in knb_games:
            if not _knb_game_alive(knb_games[key]):
                knb_games.pop(key, None)
            else:
                await _business_edit_message(
                    conn_id, msg.chat.id, msg.message_id,
                    "⚔️ <b>Игра уже идёт</b> — закончи её или отмени.\n"
                    f"<code>{LINE}</code>\n"
                    "◇ Отменить: кнопка «✕ Отменить» на сообщении игры.",
                )
                return
        game = {
            "mode": "dm",
            "chat_id": msg.chat.id,
            "conn_id": conn_id,
            "a_id": owner_id,
            "b_id": msg.chat.id,
            "a_name": _knb_display_name(msg.from_user.full_name or "", msg.from_user.username or ""),
            "b_name": _knb_display_name(
                getattr(msg.chat, "first_name", "") or "",
                getattr(msg.chat, "username", "") or "",
            ),
            "status": "playing",
            "turn": random.choice(["a", "b"]),
            "move_a": None,
            "move_b": None,
            "score_a": 0,
            "score_b": 0,
            "ts": asyncio.get_running_loop().time(),
        }
        knb_games[key] = game
        first_name = game["a_name"] if game["turn"] == "a" else game["b_name"]
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id,
            _knb_header(game) + f"🎲 <b>Первый начинает:</b> {first_name}",
            reply_markup=_knb_move_kb(),
        )
        log.info(f"⚔️ .knb start conn={conn_id} chat={msg.chat.id} first={game['turn']}")
        return
    if chat_type not in ("group", "supergroup"):
        return
    # ── Группа через Business: вызов .knb @user ──
    if not msg.from_user:
        return
    _knb_cache_member(msg.chat.id, msg.from_user)
    key = ("bg", conn_id, msg.chat.id)
    if key in knb_games:
        if not _knb_game_alive(knb_games[key]):
            knb_games.pop(key, None)
        else:
            await _business_edit_message(
                conn_id, msg.chat.id, msg.message_id,
                "⚔️ <b>В этой группе уже идёт игра</b> — дождись её окончания.",
            )
            return
    target = await _knb_resolve_target(msg)
    if target is None:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id,
            "⚔️ <b>Не нашёл, кого ты вызываешь.</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Напиши так: <code>.knb @username</code>\n"
            "◇ Или <b>ответь на сообщение</b> человека и напиши <code>.knb</code>",
        )
        return
    if target["id"] == msg.from_user.id:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id, "⚔️ Нельзя бросить вызов самому себе 😅"
        )
        return
    game = {
        "mode": "group",
        "chat_id": msg.chat.id,
        "conn_id": conn_id,
        "a_id": msg.from_user.id,
        "b_id": target["id"],
        "a_name": _knb_display_name(msg.from_user.full_name or "", msg.from_user.username or ""),
        "b_name": _knb_display_name(target["full_name"], target["username"]),
        "status": "challenge",
        "turn": None,
        "move_a": None,
        "move_b": None,
        "score_a": 0,
        "score_b": 0,
        "ts": asyncio.get_running_loop().time(),
    }
    knb_games[key] = game
    game["msg_id"] = msg.message_id
    await _business_edit_message(
        conn_id, msg.chat.id, msg.message_id,
        _knb_challenge_text(game),
        reply_markup=_knb_challenge_kb(),
    )
    log.info(f"⚔️ .knb challenge bg conn={conn_id} chat={msg.chat.id} by={msg.from_user.id} target={target['id']}")

@dp.message(F.text.regexp(r"(?i)^\.knb(\s+.*)?$"), F.chat.type.in_(("group", "supergroup")))
async def on_knb_challenge(msg: Message):
    """Группа (обычный бот): .knb @user — вызов на бой 1×1, все видят."""
    if not msg.from_user:
        return
    chat_id = msg.chat.id
    uid = msg.from_user.id
    _knb_cache_member(chat_id, msg.from_user)
    await db.upsert_user(uid, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(chat_id, msg.chat.title or "", msg.chat.type)
    key = ("group", chat_id)
    if key in knb_games:
        if not _knb_game_alive(knb_games[key]):
            knb_games.pop(key, None)
        else:
            await msg.reply(
                "⚔️ <b>В этой группе уже идёт игра</b> — дождись её окончания.\n"
                f"<code>{LINE}</code>\n"
                "◇ Закрыть текущую игру можно кнопкой «✕ Отменить» на её сообщении."
            )
            return
    target = await _knb_resolve_target(msg)
    if target is None:
        await msg.reply(
            "⚔️ <b>Не нашёл, кого ты вызываешь.</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Напиши так: <code>.knb @username</code>\n"
            "◇ Или <b>ответь на сообщение</b> человека и напиши <code>.knb</code>"
        )
        return
    if target["id"] == uid:
        await msg.reply("⚔️ Нельзя бросить вызов самому себе 😅")
        return
    game = {
        "mode": "group",
        "chat_id": chat_id,
        "conn_id": None,
        "a_id": uid,
        "b_id": target["id"],
        "a_name": _knb_display_name(msg.from_user.full_name or "", msg.from_user.username or ""),
        "b_name": _knb_display_name(target["full_name"], target["username"]),
        "status": "challenge",
        "turn": None,
        "move_a": None,
        "move_b": None,
        "score_a": 0,
        "score_b": 0,
        "ts": asyncio.get_running_loop().time(),
    }
    knb_games[key] = game
    try:
        sent = await msg.reply(
            _knb_challenge_text(game),
            reply_markup=_knb_challenge_kb(),
        )
    except Exception as e:
        knb_games.pop(key, None)
        log.warning(f"knb challenge reply: {e}")
        return
    game["msg_id"] = sent.message_id
    log.info(f"⚔️ .knb challenge group={chat_id} by={uid} target={target['id']}")

@dp.callback_query(F.data == "knb_accept")
async def cb_knb_accept(call: CallbackQuery):
    key = _knb_game_key_from_call(call)
    if not key or not call.message:
        await call.answer("⛔ Вызов недоступен", show_alert=True)
        return
    game = knb_games.get(key)
    if not game:
        await call.answer("⚔️ Вызов уже неактуален", show_alert=True)
        return
    if game.get("status") != "challenge":
        await call.answer("⚔️ Бой уже начался", show_alert=False)
        return
    if not _knb_game_alive(game):
        knb_games.pop(key, None)
        await call.answer("⚔️ Вызов протух — брось новый: .knb", show_alert=True)
        return
    uid = call.from_user.id
    if uid != game["b_id"]:
        await call.answer("🙅 Этот вызов не для тебя!", show_alert=False)
        return
    game["status"] = "playing"
    game["turn"] = random.choice(["a", "b"])
    _knb_cache_member(game["chat_id"], call.from_user)
    first_name = game["a_name"] if game["turn"] == "a" else game["b_name"]
    await call.answer("⚔️ Бой принят! 🥊", show_alert=False)
    await _knb_edit(
        game, call.message.message_id,
        _knb_header(game) + f"🎲 <b>Первый начинает:</b> {first_name}",
        _knb_move_kb(),
    )

@dp.callback_query(F.data.in_({"knb_r", "knb_s", "knb_p"}))
async def cb_knb_move(call: CallbackQuery):
    key = _knb_game_key_from_call(call)
    if not key or not call.message:
        await call.answer("⛔ Игра недоступна", show_alert=True)
        return
    game = knb_games.get(key)
    if not game or game.get("status") != "playing":
        await call.answer("⚔️ Игра не найдена — начни новую: .knb", show_alert=True)
        return
    uid = call.from_user.id
    move = call.data.split("_")[1]
    expected = game["a_id"] if game["turn"] == "a" else game["b_id"]
    if uid != expected:
        await call.answer("🙅 Не твой ход — жди очереди!", show_alert=False)
        return
    _knb_cache_member(game["chat_id"], call.from_user)
    if game["turn"] == "a":
        game["move_a"] = move
        game["turn"] = "b"
    else:
        game["move_b"] = move
        game["turn"] = "a"
    if game["move_a"] and game["move_b"]:
        emoji_a = _KPB_EMOJI[game["move_a"]]
        emoji_b = _KPB_EMOJI[game["move_b"]]
        if game["move_a"] == game["move_b"]:
            result = "🤝 <b>Ничья!</b>"
        elif _KPB_BEATS[game["move_a"]] == game["move_b"]:
            game["score_a"] = game.get("score_a", 0) + 1
            result = f"🏆 Побеждает: <b>{game['a_name']}</b>"
        else:
            game["score_b"] = game.get("score_b", 0) + 1
            result = f"🏆 Побеждает: <b>{game['b_name']}</b>"
        await _knb_edit(
            game, call.message.message_id,
            _knb_header(game)
            + f"🆚 {game['a_name']} {emoji_a}  vs  {emoji_b} {game['b_name']}\n\n"
            + f"{result}",
            _knb_result_kb(),
        )
        await call.answer("🎉 Итог готов!", show_alert=False)
        return
    mover_name = game["a_name"] if uid == game["a_id"] else game["b_name"]
    next_name = game["a_name"] if game["turn"] == "a" else game["b_name"]
    await _knb_edit(
        game, call.message.message_id,
        _knb_header(game)
        + f"✅ <b>{mover_name}</b> сделал ход 🤫\n\n"
        + f"🎯 Теперь очередь: <b>{next_name}</b>",
        _knb_move_kb(),
    )
    await call.answer("🤫 Ход записан", show_alert=False)

@dp.callback_query(F.data == "knb_again")
async def cb_knb_again(call: CallbackQuery):
    key = _knb_game_key_from_call(call)
    if not key or not call.message:
        await call.answer("⛔ Игра недоступна", show_alert=True)
        return
    game = knb_games.get(key)
    if not game:
        await call.answer("⚔️ Игра не найдена", show_alert=True)
        return
    uid = call.from_user.id
    if uid not in (game["a_id"], game["b_id"]):
        await call.answer("🙅 Реванш могут назначить только игроки", show_alert=False)
        return
    game["move_a"] = None
    game["move_b"] = None
    game["status"] = "playing"
    game["turn"] = random.choice(["a", "b"])
    first_name = game["a_name"] if game["turn"] == "a" else game["b_name"]
    await _knb_edit(
        game, call.message.message_id,
        _knb_header(game) + f"🎲 <b>Первый начинает:</b> {first_name}",
        _knb_move_kb(),
    )
    await call.answer("🔁 Новый раунд", show_alert=False)

@dp.callback_query(F.data == "knb_cancel")
async def cb_knb_cancel(call: CallbackQuery):
    key = _knb_game_key_from_call(call)
    if not key or not call.message:
        await call.answer("⛔ Игра недоступна", show_alert=True)
        return
    game = knb_games.get(key)
    if not game:
        await call.answer("⚔️ Игры нет", show_alert=False)
        return
    uid = call.from_user.id
    if game.get("status") == "challenge":
        if uid not in (game["a_id"], game["b_id"]):
            await call.answer("🙅 Этот вызов не для тебя!", show_alert=False)
            return
        close_text = "⚔️ <b>Вызов отклонён</b> — бой отменён 👁️"
    else:
        if uid not in (game["a_id"], game["b_id"]):
            await call.answer("🙅 Закрыть игру могут только игроки", show_alert=False)
            return
        close_text = "⚔️ <b>Игра закрыта</b> — было жарко 👁️"
    knb_games.pop(key, None)
    await _knb_edit(
        game, call.message.message_id, close_text,
        InlineKeyboardMarkup(inline_keyboard=[]),
    )
    await call.answer("⚔️ Игра закрыта", show_alert=False)

@dp.business_message()
async def on_business_msg(msg: Message):
    if not msg.business_connection_id:
        return
    if msg.text and msg.text.lower().startswith((".ai ", ".search ", ".spam ", ".price", ".mute", ".unmute", ".nomute", ".unnomute", ".afk", ".unafk", ".code", ".uncode", ".wbl", ".unwbl", ".cmd", ".knb", ".bold ", ".italic ", ".mono ", ".line ", ".crossed ", ".hidden ", ".quote ")):
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, "save")
    if owner_id is None:
        return
    if msg.from_user and getattr(msg.chat, "type", None) in ("group", "supergroup"):
        _knb_cache_member(msg.chat.id, msg.from_user)
    if (
        msg.text
        and msg.from_user
        and msg.from_user.id == owner_id
        and msg.business_connection_id in business_code_mode
        and not msg.text.startswith(".")
    ):
        code_text = f"<pre><code>{html_escape(msg.text)}</code></pre>"
        await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, code_text)
        return
    if (
        msg.text
        and msg.from_user
        and msg.from_user.id == owner_id
        and getattr(msg.chat, "type", None) == "private"
        and (msg.business_connection_id, msg.chat.id) in business_nomute_chats
        and not msg.text.startswith(".")
    ):
        ok, retry_after, _ = await _business_send_message_ex(msg.business_connection_id, msg.chat.id, msg.text)
        if not ok and retry_after:
            await asyncio.sleep(int(retry_after))
            await _business_send_message_ex(msg.business_connection_id, msg.chat.id, msg.text)
        log.info(f"🛡️ nomute resend conn={msg.business_connection_id} chat={msg.chat.id} owner={owner_id}")
    if (
        getattr(msg.chat, "type", None) == "private"
        and msg.from_user
        and msg.from_user.id != owner_id
        and (msg.business_connection_id, msg.chat.id) in business_wbl_chats
        and _wbl_should_delete((msg.text or msg.caption or ""))
    ):
        ok, retry_after, _ = await _business_delete_message_ex(msg.business_connection_id, msg.message_id)
        if not ok and retry_after:
            await asyncio.sleep(int(retry_after))
            await _business_delete_message_ex(msg.business_connection_id, msg.message_id)
        log.debug(f"🧹 wbl deleted msg={msg.message_id} owner={owner_id}")
        return
    if (
        getattr(msg.chat, "type", None) == "private"
        and msg.from_user
        and msg.from_user.id != owner_id
        and (msg.business_connection_id, msg.chat.id) in business_muted_chats
    ):
        ok, retry_after, _ = await _business_delete_message_ex(msg.business_connection_id, msg.message_id)
        if not ok and retry_after:
            await asyncio.sleep(int(retry_after))
            await _business_delete_message_ex(msg.business_connection_id, msg.message_id)
        return
    afk = business_afk.get(msg.business_connection_id) or user_afk.get(owner_id)
    if (
        getattr(msg.chat, "type", None) == "private"
        and msg.from_user
        and msg.from_user.id != owner_id
        and afk
        and afk.get("owner_id") == owner_id
    ):
        now_mono = asyncio.get_running_loop().time()
        last = business_afk_last_reply.get((msg.business_connection_id, msg.chat.id), 0.0)
        if now_mono - last >= 45:
            parts = ["Я сейчас не в сети."]
            note = (afk.get("note") or "").strip()
            if note:
                parts.append(note)
            reply_text = "\n".join(parts)
            ok, retry_after, _ = await _business_send_message_ex(msg.business_connection_id, msg.chat.id, reply_text)
            if not ok and retry_after:
                await asyncio.sleep(int(retry_after))
                ok, _, _ = await _business_send_message_ex(msg.business_connection_id, msg.chat.id, reply_text)
            if ok:
                business_afk_last_reply[(msg.business_connection_id, msg.chat.id)] = now_mono
    media_type = "◆ Текст"
    file_id: Optional[str] = None
    for attr, label in MEDIA_MAP.items():
        obj = getattr(msg, attr, None)
        if obj:
            media_type = label
            file_id = obj[-1].file_id if attr == "photo" else (getattr(obj, "file_id", None))
            break
    await db.save_message(owner_id, {
        "msg_id":     msg.message_id,
        "sender_id":  msg.from_user.id if msg.from_user else None,
        "from_name":  msg.from_user.full_name if msg.from_user else "Неизвестно",
        "username":   f"@{msg.from_user.username}" if msg.from_user and msg.from_user.username else "",
        "chat":       msg.chat.title or getattr(msg.chat, "full_name", None) or "Личные",
        "date":       fmt_msg_date(msg.date),
        "text":       msg.text or msg.caption or "",
        "media_type": media_type,
        "file_id":    file_id,
    })
    cid = msg.chat.id
    chat_msg_ids.setdefault(cid, [])
    if msg.message_id not in chat_msg_ids[cid]:
        chat_msg_ids[cid].append(msg.message_id)
        if len(chat_msg_ids[cid]) > MAX_MSG_CACHE:
            chat_msg_ids[cid] = chat_msg_ids[cid][-MAX_MSG_CACHE:]
    log.debug(f"📥 cached msg={msg.message_id} owner={owner_id}")
@dp.edited_business_message()
async def on_edited_business_msg(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, "edit")
    if owner_id is None:
        return
    new_text = msg.text or msg.caption or ""
    is_bot_edit = (
        f"— 👁️ @{BOT_USERNAME}" in new_text
        or new_text.strip().startswith("◆")
    )
    sender_id = msg.from_user.id if msg.from_user else None
    is_owner_edit = (sender_id == owner_id)
    if not is_bot_edit and not is_owner_edit:
        cached = await db.get_message(owner_id, msg.message_id)
        old_text = cached["text"] if cached else None
        sender = fmt_sender(
            msg.from_user.full_name if msg.from_user else "Неизвестно",
            f"@{msg.from_user.username}" if msg.from_user and msg.from_user.username else "",
        )
        chat_name = msg.chat.title or getattr(msg.chat, "full_name", None) or "Личные"
        notify = (
            f"✦ <b>Сообщение отредактировано</b>\n"
            f"{LINE}\n"
            f"◇ <b>{html_escape(sender)}</b>\n"
            f"◆ {html_escape(chat_name)}\n"
            f"◷ {fmt_msg_date(msg.date)}\n"
            f"{LINE}\n"
        )
        if old_text:
            notify += f"◇ <b>Было:</b>\n{html_escape(old_text)}\n\n"
        else:
            notify += "◇ <b>Было:</b> <i>нет в архиве</i>\n\n"
        notify += f"◆ <b>Стало:</b>\n{html_escape(new_text)}"
        save_id = await db.save_intercepted(owner_id, {
            "from_name":  msg.from_user.full_name if msg.from_user else "Неизвестно",
            "username":   f"@{msg.from_user.username}" if msg.from_user and msg.from_user.username else "",
            "chat":       chat_name,
            "date":       fmt_msg_date(msg.date),
            "text":       new_text,
            "media_type": "◆ Текст",
            "file_id":    None,
            "event_type": "edited",
            "old_text":   old_text,
        })
        await db.record_stat("caught_edited")
        await _send_notify(owner_id, notify, reply_markup=kb_notify(save_id))
    media_type = "◆ Текст"
    file_id: Optional[str] = None
    for attr, label in MEDIA_MAP.items():
        obj = getattr(msg, attr, None)
        if obj:
            media_type = label
            file_id = obj[-1].file_id if attr == "photo" else (getattr(obj, "file_id", None))
            break
    await db.save_message(owner_id, {
        "msg_id":     msg.message_id,
        "sender_id":  msg.from_user.id if msg.from_user else None,
        "from_name":  msg.from_user.full_name if msg.from_user else "Неизвестно",
        "username":   f"@{msg.from_user.username}" if msg.from_user and msg.from_user.username else "",
        "chat":       msg.chat.title or getattr(msg.chat, "full_name", None) or "Личные",
        "date":       fmt_msg_date(msg.date),
        "text":       new_text,
        "media_type": media_type,
        "file_id":    file_id,
    })
    log.debug(f"✏️ updated msg={msg.message_id} owner={owner_id} bot_edit={is_bot_edit}")
_WHISPER_FILE_MAP = {
    ".oga": ("voice.ogg", "audio/ogg"),
    ".ogg": ("voice.ogg", "audio/ogg"),
    ".mp4": ("video_note.mp4", "video/mp4"),
}

# ── Сворачивание расшифровок (кнопка «Тык») ──────────────────
_TRANSCRIPT_MAX_MSG     = 4000   # безопасный лимит Telegram на сообщение
_TRANSCRIPT_CACHE: dict[str, dict] = {}
_TRANSCRIPT_CACHE_MAX  = 300

def _chunk_text(text: str, size: int = _TRANSCRIPT_MAX_MSG) -> list[str]:
    """Режет текст на куски по границе строк, не разрывая HTML-сущности."""
    if len(text) <= size:
        return [text]
    chunks = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        while cut < len(text) and "&" in text[max(0, cut - 12):cut] and not text[cut - 1] == ";":
            cut += 1
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks

def _cache_transcript(label: str, text: str) -> str:
    token = os.urandom(6).hex()
    _TRANSCRIPT_CACHE[token] = {"label": label, "text": text, "chunks": []}
    if len(_TRANSCRIPT_CACHE) > _TRANSCRIPT_CACHE_MAX:
        stale = list(_TRANSCRIPT_CACHE)[:len(_TRANSCRIPT_CACHE) - _TRANSCRIPT_CACHE_MAX]
        for k in stale:
            _TRANSCRIPT_CACHE.pop(k, None)
    return token

def _tsc_teaser(label: str) -> str:
    return f"🎤 <b>Расшифровка {label}:</b>\n{LINE}\n👆 <b>Тык</b> — откроется полный текст"

def _tsc_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👆 Тык", callback_data=f"show_tsc_{token}")],
    ])

async def _transcribe_voice(file_id: str) -> Optional[str]:
    try:
        file = await bot.get_file(file_id)
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return None
                audio_bytes = await resp.read()
        import io
        ext = os.path.splitext(file.file_path or "")[1].lower()
        fname, content_type = _WHISPER_FILE_MAP.get(ext, ("voice.ogg", "audio/ogg"))
        for api_key in GROQ_API_KEYS:
            try:
                form = aiohttp.FormData()
                form.add_field("file", io.BytesIO(audio_bytes), filename=fname, content_type=content_type)
                form.add_field("model", "whisper-large-v3")
                form.add_field("response_format", "text")
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        data=form,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status == 200:
                            await db.record_stat("whisper_ok")
                            return (await resp.text()).strip()
                        _whisper_err = (await resp.text())[:300]
                        log.warning(
                            f"Whisper key failed (status={resp.status}, body={_whisper_err}) — пробую следующий ключ"
                        )
            except Exception as e:
                log.warning(f"Whisper transcribe (key attempt): {e}")
        await db.record_stat("whisper_fail")
    except Exception as e:
        log.warning(f"Whisper transcribe: {e}")
    return None
async def _send_media(owner_id: int, file_id: str, mt: str):
    try:
        if "Фото"      in mt: await bot.send_photo(owner_id, file_id)
        elif "Видео"   in mt: await bot.send_video(owner_id, file_id)
        elif "Голос"   in mt: await bot.send_voice(owner_id, file_id)
        elif "Кружок"  in mt: await bot.send_video_note(owner_id, file_id)
        elif "Документ" in mt: await bot.send_document(owner_id, file_id)
        elif "GIF"     in mt: await bot.send_animation(owner_id, file_id)
        elif "Стикер"  in mt: await bot.send_sticker(owner_id, file_id)
    except Exception as e:
        log.warning(f"Media send: {e}")
@dp.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted):
    log.debug(f"🚨 deleted conn={event.business_connection_id} ids={event.message_ids}")
    owner_id = await _get_owner_id_cached(event.business_connection_id, "delete")
    if owner_id is None:
        return
    for msg_id in event.message_ids:
        cached = await db.get_message(owner_id, msg_id)
        if not cached:
            log.info(f"❓ msg={msg_id} not in cache for owner={owner_id} — skip, nothing to show")
            continue
        if cached.get("sender_id") == owner_id:
            log.info(f"⏭ skip own deleted msg={msg_id} owner={owner_id}")
            continue
        sender = fmt_sender(cached["from_name"], cached["username"])
        text = (
            f"✕ <b>Удалённое сообщение</b>\n"
            f"{LINE}\n"
            f"◇ <b>{sender}</b>\n"
            f"   удалил(а) сообщение\n"
            f"{LINE}\n"
            f"◆ Чат: {cached['chat']}\n"
            f"◷ Время: {cached['date']}\n"
            f"◇ Тип: {cached['media_type']}"
        )
        if cached["text"]:
            text += f"\n{LINE}\n◆ <b>Содержимое:</b>\n{cached['text']}"
        save_id = await db.save_intercepted(owner_id, {
            "from_name":  cached["from_name"],
            "username":   cached["username"],
            "chat":       cached["chat"],
            "date":       cached["date"],
            "text":       cached["text"],
            "media_type": cached["media_type"],
            "file_id":    cached["file_id"],
            "event_type": "deleted",
            "old_text":   None,
        })
        await db.record_stat("caught_deleted")
        sent_id = await _send_notify(owner_id, text, reply_markup=kb_notify(save_id))
        if sent_id is None:
            continue
        if cached["file_id"]:
            await _send_media(owner_id, cached["file_id"], cached["media_type"])
            if "Голос" in cached["media_type"] and cached["file_id"]:
                transcript = await _transcribe_voice(cached["file_id"])
                if transcript:
                    try:
                        await bot.send_message(
                            owner_id,
                            f"◆ <b>Расшифровка голосового:</b>\n{LINE}\n{html_escape(transcript)}"
                        )
                    except Exception:
                        pass
@dp.callback_query(F.data.startswith("show_tsc_"))
async def cb_show_transcript(call: CallbackQuery):
    token = call.data[len("show_tsc_"):]
    await call.answer()
    if not call.message:
        return
    entry = _TRANSCRIPT_CACHE.get(token)
    if not entry:
        try:
            await call.message.edit_text("😔 Текст расшифровки больше недоступен.")
        except Exception:
            pass
        return
    label = entry["label"]
    full = f"🎤 <b>Расшифровка {label}:</b>\n{LINE}\n{html_escape(entry['text'])}"
    hide_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔼 Скрыть", callback_data=f"hide_tsc_{token}")],
    ])
    if len(full) <= _TRANSCRIPT_MAX_MSG:
        entry["chunks"] = []
        try:
            await call.message.edit_text(full, reply_markup=hide_kb)
        except Exception as e:
            log.error(f"show transcript edit: {e}")
        return
    entry["chunks"] = []
    parts = _chunk_text(html_escape(entry["text"]), _TRANSCRIPT_MAX_MSG - 60)
    try:
        await call.message.edit_text(
            f"🎤 <b>Расшифровка {label}:</b>\n{LINE}\n{parts[0]}",
            reply_markup=hide_kb,
        )
    except Exception as e:
        log.error(f"show transcript edit (long): {e}")
    for part in parts[1:]:
        try:
            sent = await bot.send_message(call.message.chat.id, part)
            entry["chunks"].append(sent.message_id)
        except Exception:
            try:
                sent = await bot.send_message(call.message.chat.id, part, parse_mode=None)
                entry["chunks"].append(sent.message_id)
            except Exception as e:
                log.error(f"show transcript chunk: {e}")
@dp.callback_query(F.data.startswith("hide_tsc_"))
async def cb_hide_transcript(call: CallbackQuery):
    token = call.data[len("hide_tsc_"):]
    await call.answer()
    if not call.message:
        return
    entry = _TRANSCRIPT_CACHE.get(token)
    label = (entry or {}).get("label", "голосового")
    for mid in (entry or {}).get("chunks", []):
        try:
            await bot.delete_message(call.message.chat.id, mid)
        except Exception:
            pass
    if entry:
        entry["chunks"] = []
    try:
        await call.message.edit_text(
            _tsc_teaser(label),
            reply_markup=_tsc_kb(token),
        )
    except Exception as e:
        log.error(f"hide transcript: {e}")
@dp.callback_query(F.data == "ai_open")
async def cb_ai_open(call: CallbackQuery, state: FSMContext):
    await state.set_state(S.ai_chat)
    await call.answer()
    await call.message.edit_text(
        f"◆ <b>ИИ-консьерж</b>\n{LINE}\n"
        f"Модель: <b>Llama 4 Maverick · Vision</b>\n"
        f"Лимит: <b>без ограничений</b>\n\n"
        "Спрашивай что угодно — отвечу тихо и быстро ◆",
        reply_markup=kb_ai(),
    )
THINKING_FRAMES = ["◜ 👁️ Думаю", "◝ 👁️ Думаю", "◞ 👁️ Думаю", "◟ 👁️ Думаю"]
THINKING_INTERVAL = 0.4

async def _spin_thinking(chat_id: int, message_id: int):
    i = 0
    try:
        while True:
            frame = THINKING_FRAMES[i % len(THINKING_FRAMES)]
            try:
                await bot.edit_message_text(frame, chat_id=chat_id, message_id=message_id)
            except Exception:
                pass
            i += 1
            await asyncio.sleep(THINKING_INTERVAL)
    except asyncio.CancelledError:
        pass
@dp.message(S.ai_chat)
async def ai_msg(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    has_photo = bool(msg.photo)
    has_text  = bool(msg.text or msg.caption)
    if not has_text and not has_photo:
        await msg.answer("◇ Отправь текст или фото (можно с подписью).")
        return
    text_content = msg.text or msg.caption or ""
    thinking = await msg.answer(THINKING_FRAMES[0])
    spin_task = asyncio.create_task(_spin_thinking(thinking.chat.id, thinking.message_id))
    image_b64 = None
    if has_photo:
        file_id = msg.photo[-1].file_id
        image_b64 = await _get_image_base64(bot, file_id)
        if image_b64 is None:
            spin_task.cancel()
            await thinking.edit_text("◇ Не смог загрузить фото — попробуй ещё раз.")
            return
    try:
        reply = await groq_chat(uid, text_content, image_base64=image_b64)
    finally:
        spin_task.cancel()
    await thinking.delete()
    await _reply_ai_html(msg, prefix="◆ ", answer=reply, reply_markup=kb_ai())
@dp.callback_query(F.data == "ai_clear")
async def cb_ai_clear(call: CallbackQuery):
    ai_history.pop(call.from_user.id, None)
    await call.answer("✕ Диалог сброшен", show_alert=True)
@dp.callback_query(F.data == "ai_exit")
async def cb_ai_exit(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    await call.answer()
    await call.message.edit_text(
        home_text(),
        reply_markup=kb_main(uid),
    )
@dp.callback_query(F.data == "search")
async def cb_search(call: CallbackQuery, state: FSMContext):
    await state.set_state(S.ai_search)
    await call.answer()
    await call.message.edit_text(
        f"◐ <b>Поиск по архиву</b>\n{LINE}\n"
        "Введи имя, @username или ключевое слово:",
        reply_markup=kb_back("menu"),
    )
@dp.message(S.ai_search)
async def search_msg(msg: Message, state: FSMContext):
    if not msg.text:
        return
    await state.clear()
    uid     = msg.from_user.id
    results = await db.search_messages(uid, msg.text.strip())
    if not results:
        await msg.answer(
            f"◐ <b>Ничего не найдено</b> по «{msg.text}»",
            reply_markup=kb_back("menu"),
        )
        return
    lines = []
    for m in results[:15]:
        preview = (m["text"][:40] + "…") if len(m["text"] or "") > 40 else (m["text"] or m["media_type"])
        lines.append(f"◆ <b>{m['from_name']}</b>  {m['date']}\n   {preview}")
    await msg.answer(
        f"◐ <b>Найдено: {len(results)}</b>\n{LINE}\n" + "\n\n".join(lines),
        reply_markup=kb_back("menu"),
    )
@dp.callback_query(F.data.startswith("save_"))
async def cb_save_forever(call: CallbackQuery):
    msg_id = int(call.data.split("_")[1])
    uid = call.from_user.id
    cached = await db.get_message(uid, msg_id)
    if not cached:
        await call.answer("✕ Сообщение не найдено в кэше", show_alert=True)
        return
    sender = fmt_sender(cached["from_name"], cached["username"])
    save_text = (
        f"◆ <b>Сохранено из перехвата</b>\n"
        f"{LINE}\n"
        f"◇ От: <b>{sender}</b>\n"
        f"◆ Чат: {cached['chat']}\n"
        f"◷ Время: {cached['date']}\n"
        f"◇ Тип: {cached['media_type']}"
    )
    if cached["text"]:
        save_text += f"\n{LINE}\n◆ {html_escape(cached['text'])}"
    try:
        await bot.send_message(uid, save_text)
        if cached["file_id"]:
            await _send_media(uid, cached["file_id"], cached["media_type"])
        await call.answer("◆ Сохранено в архиве!", show_alert=False)
        new_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✔ Принято",      callback_data=f"ack_{msg_id}"),
                InlineKeyboardButton(text="✕ Стереть",      callback_data=f"del_{msg_id}"),
            ],
            [InlineKeyboardButton(text="◆ Сохранено",        callback_data="noop")],
            [InlineKeyboardButton(text="▣ Весь архив",       callback_data="show_all")],
        ])
        await call.message.edit_reply_markup(reply_markup=new_kb)
    except Exception as e:
        log.error(f"save_forever: {e}")
        await call.answer("✕ Не удалось сохранить", show_alert=True)
@dp.callback_query(F.data.startswith("back_"))
async def cb_back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    await call.answer()
    await call.message.edit_text(
        home_text(),
        reply_markup=kb_main(uid),
    )
@dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()
@dp.callback_query(F.data.startswith("nsave_"))
async def cb_notify_save(call: CallbackQuery):
    save_id = int(call.data.split("_")[1])
    uid     = call.from_user.id
    await call.answer("◆ Сохранено на 7 дней", show_alert=False)
    try:
        await call.message.delete()
    except Exception:
        pass
    existing_id = home_msg.get(uid)
    if existing_id:
        try:
            await bot.edit_message_text(
                home_text(), chat_id=uid, message_id=existing_id,
                reply_markup=kb_main(uid), parse_mode="HTML"
            )
            return
        except Exception:
            pass
    sent = await bot.send_message(uid, home_text(), reply_markup=kb_main(uid))
    home_msg[uid] = sent.message_id
@dp.callback_query(F.data.startswith("ndel_"))
async def cb_notify_del(call: CallbackQuery):
    save_id = int(call.data.split("_")[1])
    uid     = call.from_user.id
    await db.delete_saved_message(save_id)
    await call.answer("✕ Удалено", show_alert=False)
    try:
        await call.message.delete()
    except Exception:
        pass
    existing_id = home_msg.get(uid)
    if existing_id:
        try:
            await bot.edit_message_text(
                home_text(), chat_id=uid, message_id=existing_id,
                reply_markup=kb_main(uid), parse_mode="HTML"
            )
            return
        except Exception:
            pass
    sent = await bot.send_message(uid, home_text(), reply_markup=kb_main(uid))
    home_msg[uid] = sent.message_id
@dp.callback_query(F.data == "show_saved")
async def cb_show_saved(call: CallbackQuery):
    uid   = call.from_user.id
    items = await db.get_saved_messages(uid)
    await call.answer()
    if not items:
        await call.message.edit_text(
            f"◈ <b>Сохранённые сообщения</b>\n{LINE}\n\n"
            "Пусто.\n\n"
            "Когда придёт уведомление об удалённом\n"
            "или изменённом сообщении — нажми\n"
            "<b>«◆ Сохранить ➩»</b> и оно появится здесь.\n\n"
            "◇ Хранятся <b>7 дней</b>, затем удаляются автоматически.",
            reply_markup=kb_back("menu"),
        )
        return
    lines = []
    for item in items[:20]:
        icon = "✕" if item["event_type"] == "deleted" else "✦"
        preview = (item["text"][:35] + "…") if len(item["text"] or "") > 35 else (item["text"] or item["media_type"] or "—")
        from datetime import datetime as _dt
        try:
            days_left = (_dt.fromisoformat(item["expires_at"]) - _dt.now()).days + 1
        except Exception:
            days_left = 7
        lines.append(
            f"{icon} <b>{html_escape(item['from_name'] or '?')}</b>  {item['date']}\n"
            f"   {html_escape(preview)}  <i>({days_left} д.)</i>"
        )
    rows = []
    for item in items[:10]:
        icon = "✕" if item["event_type"] == "deleted" else "✦"
        name = (item["from_name"] or "?")[:12]
        rows.append([InlineKeyboardButton(
            text=f"✕ Удалить: {icon} {name}",
            callback_data=f"delsaved_{item['id']}"
        )])
    rows.append([InlineKeyboardButton(text="✕ Очистить все", callback_data="clearsaved")])
    rows.append([InlineKeyboardButton(text="← В меню",       callback_data="back_menu")])
    await call.message.edit_text(
        f"◈ <b>Сохранённые</b> ({len(items)})\n{LINE}\n\n"
        + "\n\n".join(lines)
        + f"\n\n{LINE}\n◇ Хранятся 7 дней от перехвата.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
@dp.callback_query(F.data.startswith("delsaved_"))
async def cb_del_saved(call: CallbackQuery):
    save_id = int(call.data.split("_")[1])
    await db.delete_saved_message(save_id)
    await call.answer("✕ Удалено")
    await cb_show_saved(call)
@dp.callback_query(F.data == "clearsaved")
async def cb_clear_saved(call: CallbackQuery):
    uid   = call.from_user.id
    items = await db.get_saved_messages(uid)
    for item in items:
        await db.delete_saved_message(item["id"])
    await call.answer("✕ Все удалены", show_alert=True)
    await call.message.edit_text(
        home_text(),
        reply_markup=kb_main(uid),
    )
@dp.callback_query(F.data == "howto")
async def cb_howto(call: CallbackQuery):
    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◆ Личный профиль (Business)", callback_data="howto_profile")],
        [InlineKeyboardButton(text="▢ Группа / Канал",            callback_data="howto_group")],
        [InlineKeyboardButton(text="← В меню",                     callback_data="back_menu")],
    ])
    await call.message.edit_text(
        f"⚙ <b>Подключение</b>\n{LINE}\n"
        "Выбери тип подключения:",
        reply_markup=kb,
    )
@dp.callback_query(F.data == "howto_profile")
async def cb_howto_profile(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"◆ <b>Подключение к профилю</b> 👁️\n"
        f"{LINE}\n\n"
        "Всего <b>3 шага</b> — и бот следит за тишиной:\n\n"
        "1️⃣ Нажми кнопку <b>«Скопировать»</b> ниже —\n"
        "   бот покажет юзернейм для копирования\n\n"
        "2️⃣ Нажми кнопку <b>«Подключить»</b> —\n"
        "   откроются настройки Telegram\n\n"
        "3️⃣ Внизу найди <b>Автоматизация чатов</b> ✦\n"
        "   и вставь скопированный юзернейм\n\n"
        f"{LINE}\n"
        "✔ Подключение доступно <b>всем</b>\n"
        "✔ После подключения удалённые и изменённые\n"
        "   сообщения будут приходить тебе мгновенно\n\n"
        "◇ <i>Свои сообщения бот не трогает — только чужие.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Скопировать", callback_data="copy_bot_username")],
            [InlineKeyboardButton(text="🔗 Подключить", url="tg://settings/edit")],
            [InlineKeyboardButton(text="← Назад", callback_data="howto")],
        ]),
    )
@dp.callback_query(F.data == "copy_bot_username")
async def cb_copy_bot_username(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"📋 <b>Юзернейм бота для копирования:</b>\n"
        f"{LINE}\n\n"
        f"<code>@{BOT_USERNAME}</code>\n\n"
        f"{LINE}\n"
        "👆 <i>Нажми и удерживай юзернейм выше —\n"
        "появится меню «Копировать»</i>\n\n"
        "Затем: <b>«Подключить»</b> → внизу\n"
        "<b>Автоматизация чатов</b> → вставить",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Подключить", url="tg://settings/edit")],
            [InlineKeyboardButton(text="← Назад", callback_data="howto_profile")],
        ]),
    )
@dp.callback_query(F.data == "howto_group")
async def cb_howto_group(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"▢ <b>Подключение к группе / каналу</b>\n{LINE}\n"
        "Бот работает бесплатно — Telegram Business не нужен!\n\n"
        f"1️⃣ Добавь <code>@{BOT_USERNAME}</code> в группу или канал\n"
        "2️⃣ Дай боту права <b>Администратора</b>\n"
        "   (нужно: читать сообщения)\n"
        "3️⃣ Для групп: отключи Privacy Mode через\n"
        "   @BotFather → /setprivacy → Disabled\n"
        f"{LINE}\n"
        "✔ Готово! Теперь в группе/канале можно\n"
        "писать <code>.ai вопрос</code> — бот ответит прямо там.\n\n"
        "◇ <i>Пример: </i><code>.ai объясни квантовую физику</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить бота в группу", url=f"https://t.me/{BOT_USERNAME}?startgroup=")],
            [InlineKeyboardButton(text="← Назад", callback_data="howto")],
        ]),
    )
@dp.callback_query(F.data == "referrals")
async def cb_referrals(call: CallbackQuery):
    uid  = call.from_user.id
    refs = await db.count_referrals(uid)
    await call.answer()
    await call.message.edit_text(
        f"⟡ <b>Приглашения</b>\n{LINE}\n"
        f"Пригласи близких — помоги проекту расти.\n\n"
        f"◇ Твоя ссылка:\n<code>{ref_link(uid)}</code>\n\n"
        f"◆ Приглашено: <b>{refs}</b>\n\n"
        "Доступ остаётся бесплатным для всех —\n"
        "приглашения помогают развивать проект.",
        reply_markup=kb_back("menu"),
    )
@dp.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery):
    uid    = call.from_user.id
    cached = await db.count_messages(uid)
    refs   = await db.count_referrals(uid)
    await call.answer()
    await call.message.edit_text(
        f"◆ <b>Твой профиль</b>\n{LINE}\n"
        f"◇ В архиве:     <b>{cached}</b>\n"
        f"◇ Приглашено:   <b>{refs}</b>\n"
        f"◇ Перехват:     <b>безлимит</b>\n"
        f"◇ Поиск:        <b>включён</b>\n"
        f"◇ ИИ:           <b>безлимит</b>\n"
        f"{LINE}\n"
        f"Quiet Mod — бесплатно и без лимитов. Навсегда.",
        reply_markup=kb_back("menu"),
    )
@dp.callback_query(F.data == "clear_cache")
async def cb_clear(call: CallbackQuery):
    count = await db.clear_messages(call.from_user.id)
    await call.answer(f"✕ Удалено {count} записей", show_alert=True)
@dp.callback_query(F.data == "show_all")
async def cb_show_all(call: CallbackQuery):
    uid      = call.from_user.id
    messages = await db.get_recent_messages(uid, 20)
    if not messages:
        await call.answer("▣ Архив пуст", show_alert=True)
        return
    lines = []
    for m in messages:
        preview = (m["text"][:40] + "…") if len(m["text"] or "") > 40 else (m["text"] or m["media_type"])
        lines.append(f"◆ <b>{m['from_name']}</b>  {m['date']}\n   {preview}")
    await call.answer()
    archive_rows = []
    archive_rows.append([InlineKeyboardButton(text="◐ Поиск по архиву", callback_data="search")])
    archive_rows.append([InlineKeyboardButton(text="✕ Очистить архив", callback_data="clear_cache")])
    archive_rows.append([InlineKeyboardButton(text="← В меню", callback_data="back_menu")])
    await call.message.edit_text(
        f"▣ <b>Последние {len(messages)} записей</b>\n{LINE}\n" + "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=archive_rows),
    )
@dp.callback_query(F.data.startswith("ack_"))
async def cb_ack(call: CallbackQuery):
    uid = call.from_user.id
    await call.answer("✔ Принято")
    await call.message.edit_text(
        home_text(),
        reply_markup=kb_main(uid),
    )
@dp.callback_query(F.data.startswith("del_"))
async def cb_del(call: CallbackQuery):
    msg_id  = int(call.data.split("_")[1])
    uid     = call.from_user.id
    await db.delete_message(uid, msg_id)
    await call.answer("✕ Удалено из архива")
    await call.message.edit_text(
        home_text(),
        reply_markup=kb_main(uid),
    )
@dp.callback_query(F.data == "donate")
async def cb_donate(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"⟡ <b>Поддержать развитие</b>\n{LINE}\n\n"
        "Quiet Mod бесплатен для всех —\n"
        "без лимитов, подписок и VIP. Навсегда.\n\n"
        "Мы никого ни о чём не просим.\n"
        "Но если у тебя есть немного лишнего —\n"
        "небольшой вклад очень поможет: серверы,\n"
        "ИИ и новые возможности.\n\n"
        "◇ <b>На что идут звёзды:</b>\n"
        "  • Стабильная работа 24/7\n"
        "  • Оплата ИИ для всех без лимитов\n"
        "  • Новые фичи и улучшения\n\n"
        "Спасибо, что ты с нами 👁️",
        reply_markup=kb_donate(),
    )
@dp.callback_query(F.data.startswith("pay_"))
async def cb_pay(call: CallbackQuery):
    parts = call.data.split("_")
    stars = int(parts[2])
    title       = f"⟡ Вклад {stars}⭐"
    description = f"Поддержка развития Quiet Mod — {stars} звёзд"
    await call.answer()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=title,
        description=description,
        payload=f"donate_{stars}",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars)],
    )
@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)
@dp.message(F.successful_payment)
async def on_payment(msg: Message):
    uid     = msg.from_user.id
    stars   = msg.successful_payment.total_amount
    payload = msg.successful_payment.invoice_payload
    await db.save_payment(uid, stars, payload)
    text = (
        f"⟡ <b>Спасибо за поддержку!</b>\n{LINE}\n\n"
        f"Ты внёс вклад в развитие Quiet Mod — <b>{stars}⭐</b>\n\n"
        "Эти средства пойдут на серверы, ИИ и новые возможности.\n\n"
        "Бот остаётся бесплатным и безлимитным для всех — навсегда.\n"
        "Именно такие люди, как ты, делают это возможным 👁️"
    )
    await msg.answer(text, reply_markup=kb_back("menu"))
    try:
        await bot.send_message(
            ADMIN_ID,
            f"⟡ <b>Донат</b> · {payload}\n"
            f"◇ {msg.from_user.full_name} (ID: {uid})\n"
            f"⭐ {stars} звёзд",
        )
    except Exception:
        pass
def _is_admin(call: CallbackQuery) -> bool:
    return call.from_user.id == ADMIN_ID
@dp.callback_query(F.data == "adm")
async def cb_adm(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call):
        await call.answer("⛔", show_alert=True)
        return
    await state.clear()
    await call.answer()
    await call.message.edit_text(
        f"▲ <b>Admin Suite</b>\n{LINE}",
        reply_markup=kb_admin(),
    )
USERS_PAGE_SIZE = 10

def _fmt_user_line(u: dict) -> str:
    uname = f"@{u['username']}" if u.get("username") else (u.get("full_name") or "—")
    if u.get("referrer_id"):
        source = f"⟡ по приглашению (от ID {u['referrer_id']})"
    else:
        source = "◇ по юзернейму / прямой запуск"
    return f"<b>{html_escape(uname)}</b>  (ID {u['id']})\n   {source}"
async def _render_users_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_users()
    offset = page * USERS_PAGE_SIZE
    users = await db.get_all_users(limit=USERS_PAGE_SIZE, offset=offset)
    if not users:
        text = f"◆ <b>Пользователи</b>\n{LINE}\nВсего: <b>{total}</b>\n\nПусто."
    else:
        lines = [_fmt_user_line(u) for u in users]
        page_count = (total + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE
        text = (
            f"◆ <b>Пользователи</b>  ({total})\n{LINE}\n\n"
            + "\n\n".join(lines)
            + f"\n\n{LINE}\nСтраница {page + 1} / {max(page_count, 1)}"
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"adm_users_p{page-1}"))
    if offset + USERS_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Вперёд →", callback_data=f"adm_users_p{page+1}"))
    rows = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="← В меню", callback_data="adm")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)
@dp.callback_query(F.data == "adm_users")
async def cb_adm_users(call: CallbackQuery):
    if not _is_admin(call): return
    await call.answer()
    text, kb = await _render_users_page(0)
    await call.message.edit_text(text, reply_markup=kb)
@dp.callback_query(F.data.startswith("adm_users_p"))
async def cb_adm_users_page(call: CallbackQuery):
    if not _is_admin(call): return
    page = int(call.data.removeprefix("adm_users_p"))
    await call.answer()
    text, kb = await _render_users_page(page)
    await call.message.edit_text(text, reply_markup=kb)
def _stat_since(days: int = 0) -> str:
    """Начало периода в UTC (naive ISO): days=0 — с начала сегодняшнего дня по МСК."""
    now_msk = datetime.now(MSK)
    start = now_msk.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    return start.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
@dp.callback_query(F.data == "adm_stats")
async def cb_adm_stats(call: CallbackQuery):
    if not _is_admin(call): return
    await call.answer()
    today = _stat_since(0)
    week  = _stat_since(6)
    month = _stat_since(29)
    async def _cnt(ev: str, since: str) -> int:
        return await db.count_stats(ev, since)
    launches = (
        await _cnt("launch", today),
        await _cnt("launch", week),
        await _cnt("launch", month),
        await db.count_stats("launch"),
    )
    users = await db.count_users()
    msgs  = await db.total_messages_all()
    stars = await db.total_stars()
    ideas = await db.count_ideas()
    del_caught = await db.count_stats("caught_deleted")
    ed_caught  = await db.count_stats("caught_edited")
    whisper    = await db.count_stats("whisper_ok")
    key_lines = []
    ok_total = 0
    for i, _k in enumerate(GROQ_API_KEYS, start=1):
        ok_a   = await db.count_stats(f"groq_key{i}_ok")
        ok_t   = await _cnt(f"groq_key{i}_ok", today)
        fail_a = await db.count_stats(f"groq_key{i}_fail")
        ok_total += ok_a
        key_lines.append(
            f"   🔑 Ключ {i}: <b>{ok_a}</b> ok (сегодня {ok_t}) · ошибок <b>{fail_a}</b>"
        )
    if not key_lines:
        key_lines = ["   — ключи не настроены —"]
    text = (
        f"◆ <b>Статистика бота</b>\n{LINE}\n"
        f"🚀 Запуски:  сегодня <b>{launches[0]}</b> · 7д <b>{launches[1]}</b> · "
        f"30д <b>{launches[2]}</b> · всего <b>{launches[3]}</b>\n"
        f"{LINE}\n"
        f"🤖 <b>Groq API</b> (успешных ответов: <b>{ok_total}</b>)\n"
        + "\n".join(key_lines)
        + f"\n{LINE}\n"
        f"✕ Перехвачено удалённых:   <b>{del_caught}</b>\n"
        f"✦ Перехвачено изменённых:  <b>{ed_caught}</b>\n"
        f"🎤 Расшифровок голосовых:  <b>{whisper}</b>\n"
        f"{LINE}\n"
        f"◇ Пользователей:  <b>{users}</b>\n"
        f"◇ Записей в БД:   <b>{msgs}</b>\n"
        f"⟡ Собрано звёзд:  <b>{stars}</b>\n"
        f"✦ Предложений:    <b>{ideas}</b>"
    )
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⟳ Обновить", callback_data="adm_stats")],
            [InlineKeyboardButton(text="← Назад", callback_data="adm")],
        ]),
    )
@dp.callback_query(F.data == "adm_ideas")
async def cb_adm_ideas(call: CallbackQuery):
    if not _is_admin(call): return
    await call.answer()
    ideas = await db.get_ideas(30)
    if not ideas:
        await call.message.edit_text(
            f"✦ <b>Предложения от пользователей</b>\n{LINE}\n"
            "Пока пусто — расскажи людям о кнопке.",
            reply_markup=kb_admin(),
        )
        return
    lines = []
    for idea in ideas[:10]:
        uname = f"@{idea['username']}" if idea['username'] else idea['full_name']
        preview = idea['text'][:80] + ("…" if len(idea['text']) > 80 else "")
        lines.append(
            f"<b>#{idea['id']}</b> · {uname}\n"
            f"   {html_escape(preview)}"
        )
    kb_rows = []
    for idea in ideas[:10]:
        kb_rows.append([InlineKeyboardButton(
            text=f"✕ Удалить #{idea['id']}",
            callback_data=f"adm_del_idea_{idea['id']}"
        )])
    kb_rows.append([InlineKeyboardButton(text="✕ Очистить все", callback_data="adm_clear_ideas")])
    kb_rows.append([InlineKeyboardButton(text="← Назад", callback_data="adm")])
    await call.message.edit_text(
        f"✦ <b>Предложения от пользователей</b>  ({len(ideas)} шт.)\n{LINE}\n\n"
        + "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
@dp.callback_query(F.data.startswith("adm_del_idea_"))
async def cb_adm_del_idea(call: CallbackQuery):
    if not _is_admin(call): return
    idea_id = int(call.data.split("_")[-1])
    await db.delete_idea(idea_id)
    await call.answer(f"✕ Предложение #{idea_id} удалено")
    await cb_adm_ideas(call)
@dp.callback_query(F.data == "adm_clear_ideas")
async def cb_adm_clear_ideas(call: CallbackQuery):
    if not _is_admin(call): return
    await db.clear_ideas()
    await call.answer("✕ Все предложения очищены", show_alert=True)
    await call.message.edit_text(
        f"✦ <b>Предложения от пользователей</b>\n{LINE}\n"
        "Список очищен.",
        reply_markup=kb_admin(),
    )
@dp.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call): return
    await call.answer()
    await state.set_state(S.broadcast)
    await call.message.edit_text(
        f"▤ <b>Сообщение всем пользователям</b>\n{LINE}\n\n"
        "Отправь сообщение, которое получат <b>все</b>,\n"
        "кто хоть раз писал /start боту.\n\n"
        "Поддерживаются текст, фото, видео и другие медиа\n"
        "с подписью — формат сохранится.\n\n"
        "✕ Для отмены — нажми кнопку ниже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="adm")]
        ]),
    )
@dp.message(S.broadcast)
async def on_broadcast_input(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        await state.clear()
        return
    await state.clear()
    ids = await db.all_user_ids()
    status = await msg.answer(f"▤ Рассылка начата · 0 / {len(ids)}…")
    ok = 0
    fail = 0
    for i, uid in enumerate(ids, start=1):
        try:
            await msg.copy_to(chat_id=uid)
            ok += 1
        except Exception as e:
            fail += 1
            log.warning(f"broadcast to {uid}: {e}")
        await asyncio.sleep(0.05)
        if i % 25 == 0 or i == len(ids):
            try:
                await status.edit_text(f"▤ Рассылка идёт · {i} / {len(ids)}…")
            except Exception:
                pass
    await status.edit_text(
        f"▤ <b>Рассылка завершена</b>\n{LINE}\n"
        f"✔ Доставлено: <b>{ok}</b>\n"
        f"✕ Не доставлено: <b>{fail}</b>",
        reply_markup=kb_admin(),
    )
@dp.callback_query(F.data == "suggest_idea")
async def cb_suggest_idea(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(S.suggest_idea)
    await call.message.edit_text(
        f"✦ <b>Предложить идею</b>\n{LINE}\n\n"
        "Расскажи, что бы ты хотел видеть в боте.\n"
        "Любая идея — полезная функция, улучшение\n"
        "интерфейса, новая команда — всё приветствуется.\n\n"
        "◇ Напиши своё предложение:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="back_menu")]
        ]),
    )
@dp.message(S.suggest_idea)
async def on_idea_input(msg: Message, state: FSMContext):
    uid   = msg.from_user.id
    text  = msg.text or msg.caption or ""
    if not text.strip():
        await msg.answer("◇ Напиши текст идеи — пустое сообщение не принято.")
        return
    await state.clear()
    await db.save_idea(
        uid,
        msg.from_user.username or "",
        msg.from_user.full_name or "",
        text.strip()
    )
    await msg.answer(
        f"✦ <b>Спасибо за идею!</b>\n{LINE}\n\n"
        "Твоё предложение отправлено разработчику.\n"
        "Лучшие идеи попадают в следующие обновления.\n\n"
        "Ты помогаешь сделать Quiet Mod лучше.",
        reply_markup=kb_back("menu"),
    )
    uname = f"@{msg.from_user.username}" if msg.from_user.username else msg.from_user.full_name
    try:
        await bot.send_message(
            ADMIN_ID,
            f"✦ <b>Новая идея!</b>\n{LINE}\n"
            f"◇ {uname} (ID: {uid})\n\n"
            f"◇ {html_escape(text[:500])}",
        )
    except Exception:
        pass
@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):
    chat = update.chat
    new_status = update.new_chat_member.status
    if new_status in ("member", "administrator", "restricted"):
        was_added = new_status != "restricted" or update.old_chat_member.status in ("left", "kicked")
        if was_added or new_status == "administrator" or new_status == "member":
            await db.add_bot_chat(chat.id, chat.title or chat.full_name or "", chat.type)
            if new_status == "restricted":
                log.info(f"📌 Бот ограничен в {chat.type} «{chat.title or chat.full_name or chat.id}» (ID: {chat.id}) — оставлен в списке")
            else:
                log.info(f"📌 Бот добавлен в {chat.type} «{chat.title or chat.full_name or chat.id}» (ID: {chat.id})")
    elif new_status in ("left", "kicked"):
        await db.remove_bot_chat(chat.id)
        log.info(f"📌 Бот удалён из {chat.type} «{chat.title or chat.full_name or chat.id}» (ID: {chat.id})")

@dp.callback_query(F.data == "adm_broadcast_groups")
async def cb_adm_broadcast_groups(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call):
        return
    await call.answer()
    chats = await db.get_all_bot_chats()
    if not chats:
        await call.message.edit_text(
            f"▤ <b>Рассылка по группам/каналам</b>\n{LINE}\n\n"
            "Бот пока не добавлен ни в одну группу или канал.\n\n"
            "Добавь бота в группу/канал и выдай права\n"
            "администратора — после этого чат появится в\n"
            "списке и сюда можно будет делать рассылку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="← Назад", callback_data="adm")]
            ]),
        )
        return
    await state.set_state(S.broadcast_groups)
    chat_list = "\n".join(
        f"◇ {c['title'] or '—'} ({c['chat_type']}, ID: {c['id']})"
        for c in chats
    )
    await call.message.edit_text(
        f"▤ <b>Рассылка по группам/каналам</b>\n{LINE}\n\n"
        f"Бот админ в <b>{len(chats)}</b> чатах:\n"
        f"{chat_list}\n\n"
        f"{LINE}\n"
        "Отправь сообщение — оно будет скопировано\n"
        "во все чаты, где бот администратор.\n\n"
        "Поддерживаются текст, фото, видео и другие\n"
        "медиа с подписью — формат сохранится.\n\n"
        "✕ Для отмены — нажми кнопку ниже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="adm")]
        ]),
    )

@dp.message(F.text.regexp(r"(?i)^\.cmd$"), F.chat.type.in_({"private", "group", "supergroup", "channel"}))
async def on_cmd(msg: Message):
    await msg.answer(
        f"◆ <b>QUIET MOD</b> 👁️ — список команд\n{LINE}\n\n"
        "Выбери команду:",
        reply_markup=kb_cmd(),
    )

@dp.business_message(F.text.regexp(r"(?i)^\.cmd$"))
async def on_cmd_business(msg: Message):
    await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        f"◆ <b>QUIET MOD</b> 👁️ — список команд"
    )
    await _business_send_message_ex(
        msg.business_connection_id, msg.chat.id,
        f"◆ <b>QUIET MOD</b> 👁️ — список команд\n{LINE}\n\n"
        "Выбери команду:"
    )

@dp.callback_query(F.data.startswith("cmd_info_"))
async def cb_cmd_info(call: CallbackQuery):
    key = call.data.replace("cmd_info_", "")
    feat = CMD_FEATURES.get(key)
    if not feat:
        await call.answer("Функция не найдена", show_alert=True)
        return
    text = (
        f"{feat['title']}\n{LINE}\n\n"
        f"{feat['desc']}\n\n"
        f"<b>Использование:</b>\n<code>{feat['usage']}</code>\n\n"
        f"<b>Пример:</b>\n<code>{feat['example']}</code>\n\n"
        f"◇ {feat['note']}"
    )
    await call.answer()
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← К списку", callback_data="cmd_back")],
        [InlineKeyboardButton(text="✕ Закрыть", callback_data="cmd_close")],
    ]))

@dp.callback_query(F.data == "cmd_back")
async def cb_cmd_back(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"◆ <b>QUIET MOD</b> 👁️ — список функций\n{LINE}\n\n"
        "Выбери интересующую функцию:",
        reply_markup=kb_cmd(),
    )

@dp.callback_query(F.data == "cmd_close")
async def cb_cmd_close(call: CallbackQuery):
    await call.answer("✕ Закрыто")
    try:
        await call.message.delete()
    except Exception:
        pass

@dp.message(S.broadcast_groups)
async def on_broadcast_groups_input(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        await state.clear()
        return
    await state.clear()
    chats = await db.get_all_bot_chats()
    if not chats:
        await msg.answer(
            "▤ Нет чатов для рассылки — бот нигде не админ.",
            reply_markup=kb_admin(),
        )
        return
    status = await msg.answer(f"▤ Рассылка по группам/каналам · 0 / {len(chats)}…")
    ok = 0
    fail = 0
    removed = 0
    for i, chat in enumerate(chats, start=1):
        try:
            await msg.copy_to(chat_id=chat["id"])
            ok += 1
        except Exception as e:
            err_str = str(e).lower()
            if "group chat was upgraded" in err_str or "chat not found" in err_str or "migrated" in err_str:
                await db.remove_bot_chat(chat["id"])
                removed += 1
                log.info(f"🧹 Удалён устаревший чат {chat['id']} ({chat.get('title', '?')}) из БД")
            else:
                fail += 1
                log.warning(f"broadcast_groups to {chat['id']} ({chat.get('title', '?')}): {e}")
        await asyncio.sleep(0.05)
        if i % 10 == 0 or i == len(chats):
            try:
                await status.edit_text(f"▤ Рассылка по группам/каналам · {i} / {len(chats)}…")
            except Exception:
                pass
    result_parts = [f"✔ Доставлено: <b>{ok}</b>"]
    if fail:
        result_parts.append(f"✕ Ошибок: <b>{fail}</b>")
    if removed:
        result_parts.append(f"🧹 Устаревших чатов удалено: <b>{removed}</b>")
    await status.edit_text(
        f"▤ <b>Рассылка по группам/каналам завершена</b>\n{LINE}\n" + "\n".join(result_parts),
        reply_markup=kb_admin(),
    )

DEVLOG = (
    "◆ <b>QUIET MOD</b> 👁️  <code>Black Edition</code>\n"
    f"{LINE}\n\n"
    "Привет! Это краткий обзор того, что умеет бот.\n"
    "Если ты здесь впервые — добро пожаловать в тишину.\n\n"
    f"{LINE}\n"
    "▲ <b>ПЕРЕХВАТ СООБЩЕНИЙ</b>\n\n"
    "✕ <b>Удалённые сообщения</b>\n"
    "   Кто-то удалил сообщение в переписке?\n"
    "   Бот мгновенно пришлёт тебе его содержимое:\n"
    "   текст, фото, видео, голосовое, стикер, GIF.\n\n"
    "✦ <b>Изменённые сообщения</b>\n"
    "   Отредактировали сообщение после отправки?\n"
    "   Увидишь сразу — что <i>было</i> и что <i>стало</i>.\n\n"
    "◇ <b>Умный фильтр</b>\n"
    "   Свои удалённые и изменённые — тишина.\n"
    "   Только чужие. Никакого лишнего шума.\n\n"
    f"{LINE}\n"
    "◆ <b>ИИ-КОНСЬЕРЖ</b>  <i>(без лимитов)</i>\n\n"
    "◇ <b>Чат с ИИ прямо в боте</b>\n"
    "   Задай любой вопрос — ИИ ответит чётко и быстро.\n"
    "   История диалога сохраняется до сброса.\n\n"
    "◇ <b>Анализ изображений</b>\n"
    "   Прикрепи фото — ИИ разберёт, прочитает текст,\n"
    "   решит задачу или объяснит что на картинке.\n\n"
    "◇ <b>ИИ в группах и каналах</b>\n"
    "   Добавь бота в любой чат, напиши:\n"
    "   <code>.ai вопрос</code> — бот ответит прямо в беседе.\n\n"
    "◇ <b>ИИ в бизнес-переписке</b>\n"
    "   Напиши <code>.ai вопрос</code> прямо в чате с собеседником —\n"
    "   бот незаметно заменит твоё сообщение ответом.\n\n"
    "◇ <b>Расшифровка голосовых</b>\n"
    "   Удалённое голосовое автоматически расшифруется\n"
    "   в текст. Whisper AI — точность 95%+.\n\n"
    f"{LINE}\n"
    "▣ <b>АРХИВ СООБЩЕНИЙ</b>\n\n"
    "◇ <b>Хранилище перехватов</b>\n"
    "   Все перехваченные сообщения хранятся в архиве.\n"
    "   Архив безлимитный — для всех.\n\n"
    "◐ <b>Поиск по архиву</b>\n"
    "   Найди любое сообщение по тексту, имени\n"
    "   отправителя или юзернейму за секунды.\n\n"
    "◆ <b>Сохранить навсегда</b>\n"
    "   Одна кнопка под уведомлением — и сообщение\n"
    "   останется у тебя навсегда вне зависимости от архива.\n\n"
    f"{LINE}\n"
    "⚔️ <b>МИНИ-ИГРЫ</b>\n\n"
    "◇ <b>Камень · Ножницы · Бумага</b>\n"
    "   <code>.knb</code> в личном чате — играй 1×1;\n"
    "   в группе — <code>.knb @user</code> и кнопка\n"
    "   «⚔️ Принять бой». Секретные ходы,\n"
    "   случайный первый ход, счёт на реваншах.\n\n"
    "💤 <b>AFK</b> — автоответ «не в сети»:\n"
    "   <code>.afk</code> или <code>.afk заметка</code> в ЛС с ботом,\n"
    "   выключить — кнопка «🔴 Выключить AFK».\n\n"
    f"{LINE}\n"
    "⟡ <b>ПОДДЕРЖКА ПРОЕКТА</b>\n\n"
    "   Quiet Mod бесплатен и без лимитов — навсегда.\n"
    "   Мы никого ни о чём не просим.\n\n"
    "   Но если у тебя есть немного лишнего —\n"
    "   вклад 15/30/50⭐ очень поможет: серверы,\n"
    "   ИИ и новые возможности.\n\n"
    "   В меню бота: <b>«⟡ Поддержать проект»</b>\n\n"
    f"{LINE}\n"
    "⚙ <b>КАК ПОДКЛЮЧИТЬ?</b>\n\n"
    "   Нужен <b>Telegram Business</b> (или просто добавить\n"
    "   бота в группу для ИИ-функций).\n"
    "   В боте есть кнопка <b>«Подключение»</b> — там\n"
    "   пошаговая инструкция с картинками.\n\n"
    f"{LINE}\n"
    "▲ <b>ВПЕРЕДИ — ЕЩЁ БОЛЬШЕ</b>\n\n"
    "   Бот активно развивается. В планах:\n"
    "   — Уведомления о скриншотах\n"
    "   — Статистика активности чатов\n"
    "   — Экспорт архива в файл\n"
    "   — Ещё больше ИИ-возможностей\n\n"
    "◇ Есть идея? Нажми кнопку <b>«✦ Предложить»</b> в боте.\n"
    "   Лучшие идеи от вас — уже в следующем обновлении.\n\n"
    f"{LINE}\n"
    "Спасибо что ты здесь. Это только начало.\n"
    "— Команда <b>Quiet Mod</b> 👁️"
)

async def _broadcast_devlog():
    ids = await db.all_user_ids()
    ok = 0
    fail = 0
    for uid in ids:
        try:
            await bot.send_message(uid, DEVLOG)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    log.info(f"📢 DevLog разослан: ok={ok} fail={fail}")
    try:
        await bot.send_message(ADMIN_ID, f"▤ DevLog разослан: ✔ {ok} · ✕ {fail}")
    except Exception:
        pass

@dp.message(F.chat.type.in_({"group", "supergroup", "channel"}))
@dp.channel_post()
async def on_group_msg(msg: Message):
    """Сохраняет чат в БД при любом сообщении в группе/канале."""
    if msg.chat.type in ("group", "supergroup", "channel"):
        await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
        if msg.from_user:
            await db.upsert_user(
                msg.from_user.id,
                msg.from_user.username or "",
                msg.from_user.full_name or "",
            )
            _knb_cache_member(msg.chat.id, msg.from_user)
    if msg.voice or msg.video_note:
        media_label = "голосового" if msg.voice else "кружка"
        try:
            thinking = await msg.reply("🎤 Расшифровываю…")
        except Exception as e:
            log.error(f"group voice thinking reply: {e}")
            return
        try:
            file_id = (msg.voice or msg.video_note).file_id
            transcript = await _transcribe_voice(file_id)
            if transcript:
                token = _cache_transcript(media_label, transcript)
                try:
                    await thinking.edit_text(
                        _tsc_teaser(media_label),
                        reply_markup=_tsc_kb(token),
                    )
                except Exception as e:
                    log.error(f"group voice teaser edit: {e}")
            else:
                await _edit_ai_html(
                    thinking,
                    prefix="",
                    answer="😔 <b>Не удалось расшифровать</b> — попробуй ещё раз.",
                )
        except Exception as e:
            log.error(f"group voice/video transcription: {e}")
            try:
                await _edit_ai_html(
                    thinking,
                    prefix="",
                    answer="😔 <b>Не удалось расшифровать</b> — попробуй ещё раз.",
                )
            except Exception:
                try:
                    await thinking.delete()
                except Exception:
                    pass
