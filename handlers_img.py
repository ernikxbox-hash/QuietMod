"""🎨 .img — ИИ-генерация картинок по тексту (бесплатно, без ключей).

Три режима (как у .stik/.wm):
1. ЛС с ботом: .img текст — бот генерирует картинку и возвращает её.
2. Бизнес-чат (ЛС с другом): .img текст — картинка в этот чат.
3. Группа/канал: .img текст — картинка в чат.

Генерация: бесплатный Pollinations.ai (без API-ключа) — GET
https://image.pollinations.ai/prompt/{prompt}?width=..&height=..&model=flux
возвращает JPEG прямо в теле ответа. Параметр nologo=true убирает логотип.

Дополнительно:
- .img 16:9 <текст> — формат картинки (16:9, 9:16, 3:2, 2:3, 4:5, 1:1);
  без формата — квадрат 1024×1024.
- .img x2 <текст> — картинка в 2 раза больше (2048×2048).
- Анти-спам: не чаще одной генерации в 15 секунд на юзера (картинки
  генерируются 5–30 секунд — больше не нужно).
"""
import asyncio
import time
import urllib.parse
from typing import Optional

import aiohttp
from aiogram import F
from aiogram.filters import StateFilter
from aiogram.types import BufferedInputFile, Message
from html import escape as html_escape

import database as db
from business_api import (
    _business_delete_message_ex,
    _business_edit_message,
    _get_owner_id_cached,
)
from core import BOT_USERNAME, bot, dp, get_http, log
from functions import LINE

_IMG_API = "https://image.pollinations.ai/prompt/"
_IMG_DEFAULT_SIZE = 1024
_IMG_MAX_PROMPT = 400      # лимит текста промпта (картинки не станут лучше от простыни)
_IMG_COOLDOWN_SECONDS = 15.0  # анти-спам: 1 генерация в 15 сек на юзера
_img_last_gen: dict[int, float] = {}

# Доступные форматы: (название, ширина, высота)
_IMG_RATIOS = {
    "1:1": (1024, 1024),
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "3:2": (1152, 768),
    "2:3": (768, 1152),
    "4:5": (1024, 1280),
}


def _img_parse(msg: Message) -> tuple[str, int, int]:
    """(промпт, ширина, высота) из текста команды."""
    raw = (msg.text or msg.caption or "").strip()
    body = raw[4:].strip() if len(raw) >= 4 else ""
    width, height = _IMG_DEFAULT_SIZE, _IMG_DEFAULT_SIZE
    # .img x2 <текст> — разрешение в 2 раза больше
    low = body.lower()
    if low.startswith("x2 "):
        width = height = _IMG_DEFAULT_SIZE * 2
        body = body[3:].strip()
    else:
        # .img 16:9 <текст> — формат картинки
        for ratio, (w, h) in _IMG_RATIOS.items():
            if low.startswith(ratio + " "):
                width, height = w, h
                body = body[len(ratio):].strip()
                break
    return body[:_IMG_MAX_PROMPT], width, height


def _img_key(uid: int) -> bool:
    """Анти-спам: True, если пользователю можно генерировать сейчас."""
    now = time.monotonic()
    last = _img_last_gen.get(uid, 0.0)
    if now - last < _IMG_COOLDOWN_SECONDS:
        return False
    _img_last_gen[uid] = now
    if len(_img_last_gen) > 20_000:
        _img_last_gen.clear()
    return True


async def _img_generate(prompt: str, width: int, height: int) -> Optional[bytes]:
    """Запрашивает картинку у Pollinations и возвращает JPEG. None — ошибка."""
    # quote(safe="") — корректное URL-кодирование: кириллица, пробелы, ? & #
    url = _IMG_API + urllib.parse.quote(prompt[:200], safe="")
    params = {
        "width": width,
        "height": height,
        "nologo": "true",
        "model": "flux",
    }
    session = get_http()
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=90)
        ) as resp:
            if resp.status != 200:
                log.warning(f"img api status: {resp.status}")
                return None
            data = await resp.read()
            if not data:
                return None
            return data
    except asyncio.TimeoutError:
        log.warning("img api timeout")
        return None
    except Exception as e:
        log.warning(f"img api error: {e}")
        return None


async def _img_run(prompt: str, width: int, height: int, chat_id: int,
                   business_connection_id: Optional[str] = None) -> bool:
    """Генерирует картинку и отправляет в чат. True — успех."""
    try:
        jpg = await _img_generate(prompt, width, height)
    except Exception as e:
        log.error(f"img generate: {e}")
        return False
    if not jpg:
        return False
    try:
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(jpg, filename="img.jpg"),
            caption=f"◇ <i>{html_escape(prompt[:120])}</i>",
            business_connection_id=business_connection_id,
        )
        return True
    except Exception as e:
        log.error(f"img send: {e}")
        return False


async def _img_cleanup(thinking: Optional[Message]) -> None:
    if thinking is None:
        return
    try:
        await thinking.delete()
    except Exception:
        pass


async def _img_status(prompt: str, width: int, height: int, chat_id: int, send_fn,
                      business_connection_id: Optional[str] = None) -> bool:
    """Статус-сообщение → генерация → уборка. False при ошибке (с ответом)."""
    thinking = await send_fn("◆ · · ·")
    ok = await _img_run(prompt, width, height, chat_id,
                        business_connection_id=business_connection_id)
    await _img_cleanup(thinking)
    if not ok:
        await send_fn("◇ Не получилось сгенерировать картинку — попробуй другой текст.")
    return ok


_IMG_HINT = (
    f"◆ <b>.img</b> — нарисуй что угодно ИИ\n"
    f"<code>{LINE}</code>\n\n"
    "◇ <code>.img кот в космосе</code>\n"
    "◇ <code>.img 16:9 неоновый город ночью</code>\n"
    "◇ <code>.img 9:16 аниме девушка с зонтом</code>\n"
    "◇ <code>.img x2 киберпанк портрет</code>\n\n"
    f"<code>{LINE}</code>\n"
    "◇ Форматы: 1:1 · 16:9 · 9:16 · 3:2 · 2:3 · 4:5 · x2 (больше)\n"
    "◇ Генерация занимает 5–30 секунд\n"
    f"— 👁️ @{BOT_USERNAME}"
)


# ── ЛС с ботом: .img текст ────────────────────────────────────────────
@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.img(\s+.*)?$"), F.chat.type == "private")
async def on_img_dm(msg: Message):
    if not msg.from_user:
        return
    prompt, width, height = _img_parse(msg)
    if not prompt:
        await msg.answer(_IMG_HINT)
        return
    if not _img_key(msg.from_user.id):
        await msg.answer("◇ Картинка уже генерируется — подожди пару секунд.")
        return
    await db.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    await _img_status(prompt, width, height, msg.chat.id, msg.answer)


# ── Бизнес-чат (ЛС с другом): .img текст ──────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.img(\s+.*)?$"))
async def on_img_business(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return
    owner_id = await _get_owner_id_cached(conn_id, ".img")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    prompt, width, height = _img_parse(msg)
    if not prompt:
        await _business_edit_message(conn_id, msg.chat.id, msg.message_id, _IMG_HINT)
        return
    if not _img_key(owner_id):
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id,
            "◇ Картинка уже генерируется — подожди пару секунд.",
        )
        return
    ok = await _business_edit_message(conn_id, msg.chat.id, msg.message_id, "◆ · · ·")
    if not ok:
        return
    if await _img_run(prompt, width, height, msg.chat.id, business_connection_id=conn_id):
        try:
            await _business_delete_message_ex(conn_id, msg.message_id)
        except Exception:
            pass
    else:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id,
            "◇ Не получилось сгенерировать картинку — попробуй другой текст.",
        )


# ── Группа / канал: .img текст ────────────────────────────────────────
@dp.message(F.text.regexp(r"(?i)^\.img(\s+.*)?$"),
            F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_img_group(msg: Message):
    if not msg.from_user:
        return
    await db.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    prompt, width, height = _img_parse(msg)
    if not prompt:
        await msg.reply(_IMG_HINT)
        return
    if not _img_key(msg.from_user.id):
        await msg.reply("◇ Картинка уже генерируется — подожди пару секунд.")
        return
    await _img_status(prompt, width, height, msg.chat.id, msg.reply)
