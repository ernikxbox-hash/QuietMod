"""🎨 .draw — генерация картинок по описанию (Gemini 2.5 Flash Image / Nano Banana).

Отдельная команда, работает ТОЛЬКО на генерацию изображений: GEMINI_API_KEY
используется исключительно здесь и нигде больше в боте.

Лимит: 7 картинок в день на человека (счётчик в БД, переживает рестарты).
Гонка исключена per-user блокировкой: проверка лимита → генерация → инкремент
выполняются под asyncio.Lock, поэтому два параллельных .draw одного юзера не
смогут обойти лимит.

После исчерпания лимита бот отвечает тёплым текстом — без злости, с намёком
на совесть и напоминанием, что бот бесплатен для всех.

Формат API проверен: POST /v1beta/models/gemini-2.5-flash-image:generateContent,
ключ в заголовке x-goog-api-key, картинка возвращается base64 в
candidates[0].content.parts[*].inlineData.data.
"""
import asyncio
import base64
from datetime import datetime
from typing import Optional

import aiohttp
from aiogram import F
from aiogram.filters import StateFilter
from aiogram.types import BufferedInputFile, Message

import database as db
from business_api import (
    _business_delete_message_ex,
    _business_edit_message,
    _get_owner_id_cached,
)
from core import GEMINI_API_KEY, bot, dp, get_http, log
from functions import LINE, MSK

DRAW_DAILY_LIMIT = 7
DRAW_MODEL = "gemini-2.5-flash-image"
DRAW_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{DRAW_MODEL}:generateContent"
)

# Per-user блокировки: сериализуют «проверка → генерация → инкремент»,
# чтобы параллельные .draw одного юзера не обошли дневной лимит.
_draw_locks: dict[int, asyncio.Lock] = {}


def _draw_lock(uid: int) -> asyncio.Lock:
    return _draw_locks.setdefault(uid, asyncio.Lock())


DRAW_HOWTO = (
    "🎨 <b>.draw</b> — сгенерирую картинку по описанию.\n\n"
    "◇ <i>Как использовать:</i>\n"
    "   · <code>.draw кот в космосе</code>\n"
    "   · <code>.draw неоновый город, киберпанк</code>\n"
    "   · <code>.draw логотип для бренда, минимализм</code>\n\n"
    f"◇ Лимит: <b>{DRAW_DAILY_LIMIT} картинок в день</b> на человека · бесплатно\n"
    "◇ Модель: Gemini 2.5 Flash Image (Nano Banana)"
)

DRAW_LIMIT_TEXT = (
    "🎨 <b>ЛИМИТ ГЕНЕРАЦИИ ИСЧЕРПАН</b>\n"
    f"<code>{LINE}</code>\n\n"
    f"◇ Сегодня ты уже создал(а) <b>{DRAW_DAILY_LIMIT} картинок</b> —\n"
    "   дневной лимит закончился.\n"
    "◇ Завтра лимит снова будет 7 — приходи, нарисуем ещё.\n\n"
    f"<code>{LINE}</code>\n"
    "◆ Quiet Mod <b>бесплатен для всех</b> — без подписок и VIP.\n"
    "◇ Картинки рисует дорогая нейросеть Gemini — её бесплатные\n"
    "   ресурсы ограничены, поэтому 7 в день на человека.\n"
    "◇ Это не так уж и плохо, правда? Будь добр к проекту —\n"
    "   и к совести. Мы стараемся для тебя бесплатно 👁️"
)


def _draw_day() -> str:
    """Сегодняшняя дата по МСК — день лимита."""
    return datetime.now(MSK).strftime("%Y-%m-%d")


def _draw_prompt(raw_text: str) -> str:
    """Промпт из текста команды: '.draw кот в космосе' -> 'кот в космосе'."""
    if " " in raw_text:
        return raw_text[raw_text.index(" ") + 1:].strip()
    return ""


async def _draw_used(uid: int) -> int:
    return await db.get_image_usage(uid, _draw_day())


async def _gemini_generate(prompt: str) -> tuple[Optional[bytes], Optional[str]]:
    """Генерация картинки через Gemini. (байты PNG/JPEG, текст ошибки или None)."""
    if not (GEMINI_API_KEY or "").strip():
        return None, "🔑 Не настроен ключ Gemini — добавь <code>GEMINI_API_KEY</code> в переменные окружения."
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json",
    }
    try:
        session = get_http()
        async with session.post(
            DRAW_API_URL,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning(f"🎨 Gemini draw status={resp.status}: {body[:200]}")
                if resp.status == 429:
                    return None, "Генератор перегружен — подожди немного и попробуй ещё раз."
                if resp.status == 400:
                    return None, "Gemini отклонил запрос — возможно, тема запрещена. Переформулируй описание."
                return None, "Не получилось сгенерировать картинку — попробуй ещё раз."
            data = await resp.json()
    except Exception as e:
        log.warning(f"🎨 Gemini draw HTTP: {e}")
        return None, "Не получилось сгенерировать картинку — попробуй ещё раз."
    try:
        candidate = (data.get("candidates") or [{}])[0] or {}
        finish = (candidate.get("finishReason") or "").upper()
        parts = ((candidate.get("content") or {}).get("parts")) or []
        for part in parts:
            inline = (part or {}).get("inlineData") or {}
            if inline.get("data"):
                return base64.b64decode(inline["data"]), None
        if "SAFETY" in finish or "PROHIBITED" in finish:
            return None, "Gemini отклонил картинку — содержание запроса не прошло модерацию. Переформулируй описание."
    except Exception as e:
        log.warning(f"🎨 Gemini draw parse: {e}")
    return None, "Gemini не вернул картинку — попробуй переформулировать запрос."


# ── Бизнес-чат: .draw описание ────────────────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.draw(\s+.+)?$"))
async def on_draw_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".draw")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    raw_text = (msg.text or msg.caption or "").strip()
    prompt = _draw_prompt(raw_text)
    if not prompt:
        await _business_edit_message(
            msg.business_connection_id, msg.chat.id, msg.message_id, DRAW_HOWTO
        )
        return
    async with _draw_lock(owner_id):
        if await _draw_used(owner_id) >= DRAW_DAILY_LIMIT:
            await _business_edit_message(
                msg.business_connection_id, msg.chat.id, msg.message_id, DRAW_LIMIT_TEXT
            )
            return
        ok = await _business_edit_message(
            msg.business_connection_id, msg.chat.id, msg.message_id, "🎨 Рисую…"
        )
        if not ok:
            return
        img, err = await _gemini_generate(prompt)
        if img is None:
            await _business_edit_message(
                msg.business_connection_id, msg.chat.id, msg.message_id, err
            )
            return
        await db.increment_image_usage(owner_id, _draw_day())
        try:
            await bot.send_photo(
                msg.chat.id,
                photo=BufferedInputFile(img, filename="draw.png"),
                business_connection_id=msg.business_connection_id,
            )
            try:
                await _business_delete_message_ex(msg.business_connection_id, msg.message_id)
            except Exception:
                pass
        except Exception as e:
            log.error(f"🎨 .draw business send: {e}")
            await _business_edit_message(
                msg.business_connection_id, msg.chat.id, msg.message_id,
                "😔 Картинка готова, но не смог её отправить — попробуй ещё раз.",
            )
    log.info(f"🎨 .draw business owner={owner_id} prompt={prompt[:60]}")


# ── ЛС с ботом: .draw описание ────────────────────────────────────────
@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.draw(\s+.+)?$"), F.chat.type == "private")
async def on_draw_dm(msg: Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    prompt = _draw_prompt((msg.text or msg.caption or "").strip())
    if not prompt:
        await msg.answer(DRAW_HOWTO)
        return
    async with _draw_lock(uid):
        if await _draw_used(uid) >= DRAW_DAILY_LIMIT:
            await msg.answer(DRAW_LIMIT_TEXT)
            return
        thinking = await msg.answer("🎨 Рисую…")
        img, err = await _gemini_generate(prompt)
        try:
            await thinking.delete()
        except Exception:
            pass
        if img is None:
            await msg.answer(err)
            return
        await db.increment_image_usage(uid, _draw_day())
        try:
            await bot.send_photo(msg.chat.id, photo=BufferedInputFile(img, filename="draw.png"))
        except Exception as e:
            log.error(f"🎨 .draw dm send: {e}")
            await msg.answer("😔 Картинка готова, но не смог её отправить — попробуй ещё раз.")
    log.info(f"🎨 .draw dm user={uid} prompt={prompt[:60]}")


# ── Группа / канал: .draw описание ────────────────────────────────────
@dp.message(F.text.regexp(r"(?i)^\.draw(\s+.+)?$"),
            F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_draw_group(msg: Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    await db.upsert_user(uid, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    prompt = _draw_prompt((msg.text or msg.caption or "").strip())
    if not prompt:
        await msg.reply(DRAW_HOWTO)
        return
    async with _draw_lock(uid):
        if await _draw_used(uid) >= DRAW_DAILY_LIMIT:
            await msg.reply(DRAW_LIMIT_TEXT)
            return
        thinking = await msg.reply("🎨 Рисую…")
        img, err = await _gemini_generate(prompt)
        try:
            await thinking.delete()
        except Exception:
            pass
        if img is None:
            await msg.reply(err)
            return
        await db.increment_image_usage(uid, _draw_day())
        try:
            await bot.send_photo(msg.chat.id, photo=BufferedInputFile(img, filename="draw.png"))
        except Exception as e:
            log.error(f"🎨 .draw group send: {e}")
            await msg.reply("😔 Картинка готова, но не смог её отправить — попробуй ещё раз.")
    log.info(f"🎨 .draw group chat={msg.chat.id} user={uid} prompt={prompt[:60]}")
