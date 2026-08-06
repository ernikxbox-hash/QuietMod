"""🏷 .stik — делает стикер из фото (ответь на фото и напиши .stik).

Три режима (как у .ramka):
1. ЛС с ботом: .stik → бот просит фото → превращает в стикер и возвращает.
   (Если сразу ответить на фото и написать .stik — стикер сделается сразу.)
2. Бизнес-чат: ответь на фото и напиши .stik — бот вернёт стикер.
3. Группа/канал: ответь на фото и напиши .stik — стикер в чат.

Как это работает: фото скачивается, вписывается в квадрат 512×512
(стандарт статических стикеров Telegram) с прозрачными полями по бокам
для неквадратных картинок и сохраняется в WEBP. Готовый файл отправляется
через send_sticker — Telegram принимает его как обычный стикер.

Если фото больше 512×512 — вписываем целиком (не обрезаем!),
если меньше — растягиваем до квадрата, чтобы не оставалось пустоты.
"""
import asyncio
from io import BytesIO
from typing import Optional

import aiohttp
from aiogram import F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
)
from PIL import Image, ImageOps

import database as db
from business_api import (
    _business_delete_message_ex,
    _business_edit_message,
    _get_owner_id_cached,
)
from core import BOT_TOKEN, BOT_USERNAME, S, bot, dp, get_http, log
from functions import LINE

# ── Размер статического стикера Telegram (обязателен ровно 512×512) ──
_STIK_SIZE = 512


def _stik_render(data: bytes) -> bytes:
    """Фото → WEBP-стикер 512×512 (вписать целиком, поля прозрачные)."""
    im = Image.open(BytesIO(data))
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    w, h = im.size
    # вписываем целиком в квадрат, ничего не обрезая;
    # маленькие фото увеличиваем, чтобы не было пустых полей
    scale = min(_STIK_SIZE / w, _STIK_SIZE / h)
    if abs(scale - 1.0) > 0.001:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", (_STIK_SIZE, _STIK_SIZE), (0, 0, 0, 0))
    x = (_STIK_SIZE - im.width) // 2
    y = (_STIK_SIZE - im.height) // 2
    canvas.paste(im, (x, y), im)
    buf = BytesIO()
    canvas.save(buf, "WEBP", quality=92, method=6)
    return buf.getvalue()


async def _stik_process_photo(photo: PhotoSize) -> bytes:
    """Скачивает фото и возвращает WEBP-стикер (обработка в фоновом потоке)."""
    file = await bot.get_file(photo.file_id)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
    session = get_http()
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"download photo: status {resp.status}")
        data = await resp.read()
    return await asyncio.to_thread(_stik_render, data)


async def _stik_run(photo: PhotoSize, chat_id: int,
                    business_connection_id: Optional[str] = None) -> bool:
    """Делает стикер и отправляет в чат. True — успех."""
    try:
        webp = await _stik_process_photo(photo)
    except Exception as e:
        log.error(f"stik process: {e}")
        return False
    try:
        await bot.send_sticker(
            chat_id,
            sticker=BufferedInputFile(webp, filename="sticker.webp"),
            business_connection_id=business_connection_id,
        )
        return True
    except Exception as e:
        log.error(f"stik send: {e}")
        return False


async def _stik_cleanup(thinking: Optional[Message]) -> None:
    if thinking is None:
        return
    try:
        await thinking.delete()
    except Exception:
        pass


async def _stik_status(photo: PhotoSize, chat_id: int, send_fn,
                       business_connection_id: Optional[str] = None) -> bool:
    """Статус-сообщение → обработка фото → уборка. False при ошибке (с ответом)."""
    thinking = await send_fn("🏷 Делаю стикер…")
    ok = await _stik_run(photo, chat_id, business_connection_id=business_connection_id)
    await _stik_cleanup(thinking)
    if not ok:
        await send_fn("😔 Не получилось сделать стикер — попробуй другое фото.")
    return ok


def _stik_reply_photo(msg: Message) -> Optional[PhotoSize]:
    r = msg.reply_to_message
    if r and r.photo:
        return r.photo[-1]
    return None


_STIK_HINT = (
    f"🏷 <b>.stik</b> — ответь на чьё-то <b>фото</b> и напиши <code>.stik</code>,\n"
    f"◇ я сделаю из него стикер.\n\n"
    f"— 👁️ @{BOT_USERNAME}"
)


# ── ЛС с ботом: .stik → просим фото ───────────────────────────────────
@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.stik(\s+.*)?$"), F.chat.type == "private")
async def on_stik_dm(msg: Message, state: FSMContext):
    if not msg.from_user:
        return
    # сразу ответили на фото — делаем без лишних шагов
    reply_photo = _stik_reply_photo(msg)
    if reply_photo:
        await state.clear()
        await _stik_status(reply_photo, msg.chat.id, msg.answer)
        return
    await state.set_state(S.stik)
    await msg.answer(
        f"🏷 <b>СТИКЕР ИЗ ФОТО</b>\n{LINE}\n\n"
        "◇ Пришли фото — сделаю из него стикер.\n"
        "◇ Или ответь на чьё-то фото и напиши <code>.stik</code> —\n"
        "   так работает и в чатах.\n\n"
        f"— 👁️ @{BOT_USERNAME}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="stik_cancel")]
        ]),
    )


@dp.message(S.stik)
async def on_stik_photo_input(msg: Message, state: FSMContext):
    if not msg.photo:
        await msg.answer("◇ Пришли именно <b>фото</b> — из текста стикер не сделаю.")
        return
    await state.clear()
    await _stik_status(msg.photo[-1], msg.chat.id, msg.answer)


@dp.callback_query(F.data == "stik_cancel")
async def cb_stik_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("✕ Отменено", show_alert=False)
    await _stik_cleanup(call.message)


# ── Бизнес-чат: ответь на фото + .stik ────────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.stik(\s+.*)?$"))
async def on_stik_business(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return
    owner_id = await _get_owner_id_cached(conn_id, ".stik")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    photo = _stik_reply_photo(msg)
    if not photo:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id, _STIK_HINT,
        )
        return
    ok = await _business_edit_message(conn_id, msg.chat.id, msg.message_id, "🏷 Делаю стикер…")
    if not ok:
        return
    if await _stik_run(photo, msg.chat.id, business_connection_id=conn_id):
        try:
            await _business_delete_message_ex(conn_id, msg.message_id)
        except Exception:
            pass
    else:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id,
            "😔 Не получилось сделать стикер — попробуй другое фото.",
        )


# ── Группа / канал: ответь на фото + .stik ────────────────────────────
@dp.message(F.text.regexp(r"(?i)^\.stik(\s+.*)?$"),
            F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_stik_group(msg: Message):
    if not msg.from_user:
        return
    await db.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    photo = _stik_reply_photo(msg)
    if not photo:
        await msg.reply(_STIK_HINT)
        return
    await _stik_status(photo, msg.chat.id, msg.reply)
