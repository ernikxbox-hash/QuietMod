import asyncio

import logging

import os

import re

import signal

from datetime import date, datetime, timedelta, timezone

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

    InlineKeyboardButton,

    InlineKeyboardMarkup,

    LabeledPrice,

    Message,

    PreCheckoutQuery,

)



from html import escape as html_escape

import database as db



from core import (

    ADMIN_ID,

    BRAND_NAME,

    DONOR_BADGE_MIN,

    PREMIUM_MONTHLY_STARS,

    S,

    bot,

    dp,

    log,

)

from functions import *
from functions import (
    _business_edit_ai_html,
    _contains_profanity,
    _ddg_search,
    _edit_ai_html,
    _extract_city,
    _fmt_duration_ru,
    _get_image_base64,
    _get_weather,
    _groq_request,
    _is_weather_query,
    _normalize_code_blocks,
    _reply_ai_html,
    _send_notify,
    _show_home,
)



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



    is_prem = await db.is_premium(uid)

    home_text_full = (

        f"◆ <b>QUIET MOD</b> 👁️\n"

        f"<code>{LINE}</code>\n\n"

        f"<b>{html_escape(name)}</b>, добро пожаловать в тишину.\n\n"

        "Я слежу за тем, что исчезает —\n"

        "<b>удалённые и изменённые</b> сообщения\n"

        "появятся здесь раньше, чем их забудут.\n\n"

        f"<code>{LINE}</code>\n"

        f"◇ Статус       <b>{'VIP-статус' if is_prem else 'Базовый доступ'}</b>\n"

        f"◇ Перехват     <b>безлимит</b>\n"

        f"◇ Архив        <b>{'200' if is_prem else '20'} записей</b>\n"

        f"◇ ИИ           <b>без лимитов</b>\n"

        f"<code>{LINE}</code>\n\n"

        f"◇ Пригласить:\n"

        f"<code>{ref_link(uid)}</code>"

    )

    await _show_home(uid, home_text_full, kb_main(uid, is_prem), msg)











@dp.message(Command("admin"))

async def cmd_admin(msg: Message):

    if msg.from_user.id != ADMIN_ID:

        return

    await msg.answer(

        f"▲ <b>Admin Suite</b>\n{LINE}",

        reply_markup=kb_admin(),

    )

















async def _business_edit_message_ex(conn_id: str, chat_id: int, msg_id: int, text: str) -> tuple[bool, Optional[str]]:








    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"

    payload = {

        "business_connection_id": conn_id,

        "chat_id": chat_id,

        "message_id": msg_id,

        "text": text,

        "parse_mode": "HTML",

    }

    try:

        async with aiohttp.ClientSession() as session:

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





async def _business_edit_message(conn_id: str, chat_id: int, msg_id: int, text: str) -> bool:



    ok, _ = await _business_edit_message_ex(conn_id, chat_id, msg_id, text)

    return ok





async def _business_send_message_ex(conn_id: str, chat_id: int, text: str) -> tuple[bool, Optional[int], Optional[str]]:

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {

        "business_connection_id": conn_id,

        "chat_id": chat_id,

        "text": text,

    }

    try:

        async with aiohttp.ClientSession() as session:

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





async def _business_delete_message_ex(conn_id: str, msg_id: int) -> tuple[bool, Optional[int], Optional[str]]:

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteBusinessMessages"

    payload = {

        "business_connection_id": conn_id,

        "message_ids": [msg_id],

    }

    try:

        async with aiohttp.ClientSession() as session:

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



    try:

        conn = await bot.get_business_connection(msg.business_connection_id)

        owner_id = conn.user.id

    except Exception as e:

        log.error(f"get_business_connection (.spam): {e}")

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

        await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Спам уже идёт. Остановить: .spam stop")

        return



    business_spam_tasks[key] = asyncio.create_task(

        _business_spam_worker(msg.business_connection_id, msg.chat.id, owner_id, text_part, count)

    )

    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, f"◇ Запустил спам: {count}")





@dp.business_message(F.text.regexp(r"(?i)^\.mute$"))

async def on_mute_inline(msg: Message):

    if not msg.business_connection_id:

        return

    if getattr(msg.chat, "type", None) != "private":

        return

    try:

        conn = await bot.get_business_connection(msg.business_connection_id)

        owner_id = conn.user.id

    except Exception as e:

        log.error(f"get_business_connection (.mute): {e}")

        return

    if not msg.from_user or msg.from_user.id != owner_id:

        return

    business_muted_chats.add((msg.business_connection_id, msg.chat.id))

    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Mute включён")





@dp.business_message(F.text.regexp(r"(?i)^\.unmute$"))

async def on_unmute_inline(msg: Message):

    if not msg.business_connection_id:

        return

    if getattr(msg.chat, "type", None) != "private":

        return

    try:

        conn = await bot.get_business_connection(msg.business_connection_id)

        owner_id = conn.user.id

    except Exception as e:

        log.error(f"get_business_connection (.unmute): {e}")

        return

    if not msg.from_user or msg.from_user.id != owner_id:

        return

    business_muted_chats.discard((msg.business_connection_id, msg.chat.id))

    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Mute выключен")





@dp.business_message(F.text.regexp(r"(?i)^\.afk(\s+.*)?$"))

async def on_afk_inline(msg: Message):

    if not msg.business_connection_id:

        return

    try:

        conn = await bot.get_business_connection(msg.business_connection_id)

        owner_id = conn.user.id

    except Exception as e:

        log.error(f"get_business_connection (.afk): {e}")

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

    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ AFK включён")





@dp.business_message(F.text.regexp(r"(?i)^\.unafk$"))

async def on_unafk_inline(msg: Message):

    if not msg.business_connection_id:

        return

    try:

        conn = await bot.get_business_connection(msg.business_connection_id)

        owner_id = conn.user.id

    except Exception as e:

        log.error(f"get_business_connection (.unafk): {e}")

        return

    if not msg.from_user or msg.from_user.id != owner_id:

        return



    business_afk.pop(msg.business_connection_id, None)

    business_afk_last_reply.clear()

    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ AFK выключен")





@dp.business_message(F.text.regexp(r"(?i)^\.code$"))

async def on_code_inline(msg: Message):

    if not msg.business_connection_id:

        return

    try:

        conn = await bot.get_business_connection(msg.business_connection_id)

        owner_id = conn.user.id

    except Exception as e:

        log.error(f"get_business_connection (.code): {e}")

        return

    if not msg.from_user or msg.from_user.id != owner_id:

        return

    business_code_mode.add(msg.business_connection_id)

    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Code включён")





@dp.business_message(F.text.regexp(r"(?i)^\.uncode$"))

async def on_uncode_inline(msg: Message):

    if not msg.business_connection_id:

        return

    try:

        conn = await bot.get_business_connection(msg.business_connection_id)

        owner_id = conn.user.id

    except Exception as e:

        log.error(f"get_business_connection (.uncode): {e}")

        return

    if not msg.from_user or msg.from_user.id != owner_id:

        return

    business_code_mode.discard(msg.business_connection_id)

    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Code выключен")





@dp.business_message(F.text.regexp(r"(?i)^\.wbl$"))

async def on_wbl_inline(msg: Message):

    if not msg.business_connection_id:

        return

    if getattr(msg.chat, "type", None) != "private":

        return

    try:

        conn = await bot.get_business_connection(msg.business_connection_id)

        owner_id = conn.user.id

    except Exception as e:

        log.error(f"get_business_connection (.wbl): {e}")

        return

    if not msg.from_user or msg.from_user.id != owner_id:

        return

    business_wbl_chats.add((msg.business_connection_id, msg.chat.id))

    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Фильтр мата включён")





@dp.business_message(F.text.regexp(r"(?i)^\.unwbl$"))

async def on_unwbl_inline(msg: Message):

    if not msg.business_connection_id:

        return

    if getattr(msg.chat, "type", None) != "private":

        return

    try:

        conn = await bot.get_business_connection(msg.business_connection_id)

        owner_id = conn.user.id

    except Exception as e:

        log.error(f"get_business_connection (.unwbl): {e}")

        return

    if not msg.from_user or msg.from_user.id != owner_id:

        return

    business_wbl_chats.discard((msg.business_connection_id, msg.chat.id))

    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◇ Фильтр мата выключен")





@dp.business_message(F.text.regexp(r"(?i)^\.ai\s+.+"))

async def on_ai_inline(msg: Message):







    if not msg.business_connection_id:

        return



    try:

        conn = await bot.get_business_connection(msg.business_connection_id)

        owner_id = conn.user.id

    except Exception as e:

        log.error(f"get_business_connection (.ai): {e}")

        return





    if not msg.from_user or msg.from_user.id != owner_id:

        return





    raw_text = msg.text or msg.caption or ""

    question = raw_text[raw_text.index(" ") + 1:].strip() if " " in raw_text else ""





    ok = await _business_edit_message(

        msg.business_connection_id, msg.chat.id, msg.message_id,

        "◆ ·"

    )

    if not ok:

        return



    await asyncio.sleep(1)

    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◆ · ·")

    await asyncio.sleep(1)

    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◆ · · ·")

    await asyncio.sleep(1)





    image_b64 = None

    if msg.photo:

        image_b64 = await _get_image_base64(bot, msg.photo[-1].file_id)





    answer = await groq_chat(owner_id, question or "Опиши что на фото.", image_base64=image_b64)







    await _business_edit_ai_html(

        msg.business_connection_id, msg.chat.id, msg.message_id,

        prefix="", answer=f"{answer}\n\n— 👁️ @{BOT_USERNAME}"

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



    thinking = await msg.reply("◆ · · ·")



    image_b64 = None

    if msg.photo:

        image_b64 = await _get_image_base64(bot, msg.photo[-1].file_id)



    answer = await groq_chat(uid, question, image_base64=image_b64)



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



    try:

        conn = await bot.get_business_connection(msg.business_connection_id)

        owner_id = conn.user.id

    except Exception as e:

        log.error(f"get_business_connection (.search): {e}")

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



















@dp.business_message()

async def on_business_msg(msg: Message):






    if not msg.business_connection_id:

        return



    if msg.text and msg.text.lower().startswith((".ai ", ".search ", ".spam ", ".mute", ".unmute", ".afk", ".unafk", ".code", ".uncode", ".wbl", ".unwbl")):

        return



    try:

        conn = await bot.get_business_connection(msg.business_connection_id)

        owner_id = conn.user.id

    except Exception as e:

        log.error(f"get_business_connection (save): {e}")

        return



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

        getattr(msg.chat, "type", None) == "private"

        and msg.from_user

        and msg.from_user.id != owner_id

        and (msg.business_connection_id, msg.chat.id) in business_wbl_chats

        and _contains_profanity((msg.text or msg.caption or ""))

    ):

        ok, retry_after, _ = await _business_delete_message_ex(msg.business_connection_id, msg.message_id)

        if not ok and retry_after:

            await asyncio.sleep(int(retry_after))

            await _business_delete_message_ex(msg.business_connection_id, msg.message_id)

        log.info(f"🧹 wbl deleted msg={msg.message_id} owner={owner_id}")

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



    afk = business_afk.get(msg.business_connection_id)

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

            started_at = afk.get("started_at")

            elapsed = int((datetime.now(timezone.utc) - started_at).total_seconds()) if started_at else 0

            parts = [

                "Я сейчас не в сети.",

                f"Прошло: {_fmt_duration_ru(max(0, elapsed))}",

            ]

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

    log.info(f"📥 cached msg={msg.message_id} owner={owner_id}")











@dp.edited_business_message()

async def on_edited_business_msg(msg: Message):







    if not msg.business_connection_id:

        return



    try:

        conn = await bot.get_business_connection(msg.business_connection_id)

        owner_id = conn.user.id

    except Exception as e:

        log.error(f"get_business_connection (edit): {e}")

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

    log.info(f"✏️ updated msg={msg.message_id} owner={owner_id} bot_edit={is_bot_edit}")











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

        form = aiohttp.FormData()

        form.add_field("file", io.BytesIO(audio_bytes), filename="voice.ogg", content_type="audio/ogg")

        form.add_field("model", "whisper-large-v3")

        form.add_field("response_format", "text")



        async with aiohttp.ClientSession() as session:

            async with session.post(

                "https://api.groq.com/openai/v1/audio/transcriptions",

                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},

                data=form,

                timeout=aiohttp.ClientTimeout(total=30),

            ) as resp:

                if resp.status == 200:

                    return (await resp.text()).strip()

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

    log.info(f"🚨 deleted conn={event.business_connection_id} ids={event.message_ids}")

    try:

        conn = await bot.get_business_connection(event.business_connection_id)

        owner_id = conn.user.id

    except Exception as e:

        log.error(f"get_business_connection (delete): {e}")

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











@dp.callback_query(F.data == "ai_open")

async def cb_ai_open(call: CallbackQuery, state: FSMContext):

    await state.set_state(S.ai_chat)

    await call.answer()

    await call.message.edit_text(

        f"◆ <b>ИИ-консьерж</b>\n{LINE}\n"

        f"Модель: <b>Llama 4 Scout · Vision</b>\n"

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

    uid     = call.from_user.id

    is_prem = await db.is_premium(uid)

    await call.answer()

    await call.message.edit_text(

        home_text(is_prem),

        reply_markup=kb_main(uid, is_prem),

    )

















@dp.callback_query(F.data == "search")

async def cb_search(call: CallbackQuery, state: FSMContext):

    if not await db.is_premium(call.from_user.id):

        await call.answer("◈ Поиск — только для VIP", show_alert=True)

        return

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

    uid     = call.from_user.id

    is_prem = await db.is_premium(uid)

    await call.answer()

    await call.message.edit_text(

        home_text(is_prem),

        reply_markup=kb_main(uid, is_prem),

    )





@dp.callback_query(F.data == "noop")

async def cb_noop(call: CallbackQuery):

    await call.answer()











@dp.callback_query(F.data.startswith("nsave_"))

async def cb_notify_save(call: CallbackQuery):



    save_id = int(call.data.split("_")[1])

    uid     = call.from_user.id

    is_prem = await db.is_premium(uid)

    await call.answer("◆ Сохранено на 7 дней", show_alert=False)



    try:

        await call.message.delete()

    except Exception:

        pass



    existing_id = home_msg.get(uid)

    if existing_id:

        try:

            await bot.edit_message_text(

                home_text(is_prem), chat_id=uid, message_id=existing_id,

                reply_markup=kb_main(uid, is_prem), parse_mode="HTML"

            )

            return

        except Exception:

            pass

    sent = await bot.send_message(uid, home_text(is_prem), reply_markup=kb_main(uid, is_prem))

    home_msg[uid] = sent.message_id





@dp.callback_query(F.data.startswith("ndel_"))

async def cb_notify_del(call: CallbackQuery):



    save_id = int(call.data.split("_")[1])

    uid     = call.from_user.id

    is_prem = await db.is_premium(uid)

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

                home_text(is_prem), chat_id=uid, message_id=existing_id,

                reply_markup=kb_main(uid, is_prem), parse_mode="HTML"

            )

            return

        except Exception:

            pass

    sent = await bot.send_message(uid, home_text(is_prem), reply_markup=kb_main(uid, is_prem))

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

    is_prem = await db.is_premium(uid)

    await call.message.edit_text(

        home_text(is_prem),

        reply_markup=kb_main(uid, is_prem),

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

        f"◆ <b>Подключение к профилю (Business)</b>\n{LINE}\n"

        "Для этого нужен <b>Telegram Business</b> (платная подписка).\n\n"

        "1️⃣ Открой <b>Настройки Telegram</b>\n"

        "2️⃣ Перейди в <b>Telegram Business</b>\n"

        "3️⃣ Нажми <b>Автоматизация чатов</b>\n"

        f"4️⃣ Выбери <code>@{BOT_USERNAME}</code>\n"

        "5️⃣ Включи <b>Доступ к сообщениям</b>\n"

        f"{LINE}\n"

        "✔ Бот будет тихо перехватывать <b>все</b> удалённые\n"

        "и изменённые сообщения в твоих личных чатах.\n\n"

        "◇ <i>Твои собственные удалённые сообщения\n"

        "бот не присылает — только чужие.</i>",

        reply_markup=InlineKeyboardMarkup(inline_keyboard=[

            [InlineKeyboardButton(text="← Назад", callback_data="howto")],

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

    uid     = call.from_user.id

    is_prem = await db.is_premium(uid)

    cached  = await db.count_messages(uid)

    refs    = await db.count_referrals(uid)

    user    = await db.get_user(uid)

    badge   = premium_badge(is_prem, bool(user and user.get("donor_badge")))

    prem_txt = user["premium_until"] if user and user.get("premium_until") else "нет"



    await call.answer()

    await call.message.edit_text(

        f"◆ <b>Твой профиль</b> {badge}\n{LINE}\n"

        f"◇ В архиве:     <b>{cached}</b>\n"

        f"◇ Приглашено:   <b>{refs}</b>\n"

        f"◇ ИИ:           <b>безлимит</b>\n"

        f"◈ VIP до:        <b>{prem_txt}</b>\n"

        f"{LINE}\n"

        f"Лимит архива: {'200 (VIP)' if is_prem else '20 (базовый)'}",

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

    is_prem = await db.is_premium(uid)

    lines = []

    for m in messages:

        preview = (m["text"][:40] + "…") if len(m["text"] or "") > 40 else (m["text"] or m["media_type"])

        lines.append(f"◆ <b>{m['from_name']}</b>  {m['date']}\n   {preview}")

    await call.answer()



    archive_rows = []

    if is_prem:

        archive_rows.append([InlineKeyboardButton(text="◐ Поиск по архиву", callback_data="search")])

    archive_rows.append([InlineKeyboardButton(text="✕ Очистить архив", callback_data="clear_cache")])

    archive_rows.append([InlineKeyboardButton(text="← В меню", callback_data="back_menu")])

    await call.message.edit_text(

        f"▣ <b>Последние {len(messages)} записей</b>\n{LINE}\n" + "\n\n".join(lines),

        reply_markup=InlineKeyboardMarkup(inline_keyboard=archive_rows),

    )





@dp.callback_query(F.data.startswith("ack_"))

async def cb_ack(call: CallbackQuery):

    uid     = call.from_user.id

    is_prem = await db.is_premium(uid)

    await call.answer("✔ Принято")

    await call.message.edit_text(

        home_text(is_prem),

        reply_markup=kb_main(uid, is_prem),

    )





@dp.callback_query(F.data.startswith("del_"))

async def cb_del(call: CallbackQuery):

    msg_id  = int(call.data.split("_")[1])

    uid     = call.from_user.id

    is_prem = await db.is_premium(uid)

    await db.delete_message(uid, msg_id)

    await call.answer("✕ Удалено из архива")

    await call.message.edit_text(

        home_text(is_prem),

        reply_markup=kb_main(uid, is_prem),

    )











@dp.callback_query(F.data == "premium_info")

async def cb_premium_info(call: CallbackQuery):

    await call.answer()

    await call.message.edit_text(

        f"◈ <b>VIP — что даёт?</b>\n{LINE}\n"

        "◇ <b>Бесплатно навсегда:</b>\n"

        "  • Перехват удалённых и изменённых — безлимит\n"

        "  • Архив: 20 записей\n"

        "  • ИИ: безлимитно\n\n"

        "◈ <b>VIP · 50⭐/месяц:</b>\n"

        "  • Архив: 200 записей\n"

        "  • Поиск по всему архиву\n\n"

        "◇ <b>Вклад 100⭐+ (единоразово):</b>\n"

        "  • Метка в профиле\n"

        "  • +30 дней VIP в подарок\n"

        "  • Моя искренняя благодарность",

        reply_markup=kb_premium(),

    )





@dp.callback_query(F.data.startswith("pay_"))

async def cb_pay(call: CallbackQuery):

    parts = call.data.split("_")

    kind  = parts[1]

    stars = int(parts[2])



    if kind == "premium":

        title       = "◈ VIP · 1 месяц"

        description = "VIP-доступ к Quiet Mod на 30 дней"

    else:

        title       = f"◇ Вклад {stars}⭐"

        description = f"Поддержка проекта Quiet Mod — {stars} звёзд"



    await call.answer()

    await bot.send_invoice(

        chat_id=call.from_user.id,

        title=title,

        description=description,

        payload=f"{kind}_{stars}",

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

    kind = payload.split("_")[0]



    if kind == "premium":

        user    = await db.get_user(uid)

        current = user["premium_until"] if user and user.get("premium_until") else None

        if current and date.fromisoformat(current) >= date.today():

            new_date = date.fromisoformat(current) + timedelta(days=30)

        else:

            new_date = date.today() + timedelta(days=30)

        await db.set_premium(uid, new_date)

        text = (

            f"◈ <b>VIP активирован!</b>\n{LINE}\n"

            f"Действует до: <b>{new_date.strftime('%d.%m.%Y')}</b>\n"

            "Архив расширен до 200 · Поиск включён."

        )

    else:

        if stars >= DONOR_BADGE_MIN:

            await db.set_donor_badge(uid)

            bonus_date = date.today() + timedelta(days=30)

            await db.set_premium(uid, bonus_date)

            text = (

                f"◇ <b>Спасибо за поддержку!</b>\n{LINE}\n"

                f"Ты отправил <b>{stars}⭐</b>\n"

                f"Метка в профиле: ◇\n"

                f"VIP в подарок до: <b>{bonus_date.strftime('%d.%m.%Y')}</b>"

            )

        else:

            text = (

                f"◆ <b>Огромное спасибо!</b>\n{LINE}\n"

                f"Ты поддержал проект на <b>{stars}⭐</b>\n"

                "Эти средства идут на серверы и развитие."

            )



    await msg.answer(text, reply_markup=kb_back("menu"))



    try:

        await bot.send_message(

            ADMIN_ID,

            f"◈ <b>Оплата</b> · {payload}\n"

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





@dp.callback_query(F.data == "adm_stats")

async def cb_adm_stats(call: CallbackQuery):

    if not _is_admin(call): return

    users   = await db.count_users()

    msgs    = await db.total_messages_all()

    stars   = await db.total_stars()

    ideas   = await db.count_ideas()

    await call.answer()

    await call.message.edit_text(

        f"◆ <b>Общая статистика</b>\n{LINE}\n"

        f"◇ Пользователей:  <b>{users}</b>\n"

        f"◇ Записей в БД:   <b>{msgs}</b>\n"

        f"⭐ Всего звёзд:    <b>{stars}</b>\n"

        f"✦ Предложений:    <b>{ideas}</b>",

        reply_markup=InlineKeyboardMarkup(inline_keyboard=[

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



    is_prem = await db.is_premium(uid)

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

    "   Базовый: 20 записей · VIP: 200 записей.\n\n"

    "◐ <b>Поиск по архиву</b>  <i>(VIP)</i>\n"

    "   Найди любое сообщение по тексту, имени\n"

    "   отправителя или юзернейму за секунды.\n\n"

    "◆ <b>Сохранить навсегда</b>\n"

    "   Одна кнопка под уведомлением — и сообщение\n"

    "   останется у тебя навсегда вне зависимости от архива.\n\n"

    f"{LINE}\n"

    "◈ <b>VIP</b>  <code>50 звёзд / месяц</code>\n\n"

    "   • Архив расширяется с 20 до <b>200</b> записей\n"

    "   • Поиск по всему архиву\n"

    "   • Метка ◈ в профиле\n\n"

    "◇ <b>ВКЛАД</b>  <code>100+ звёзд</code>\n\n"

    "   • Метка ◇ навсегда\n"

    "   • +30 дней VIP в подарок\n"

    "   • Поддержка независимого проекта\n\n"

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
