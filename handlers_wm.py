"""🏷 .wm — водяной знак на фото (ответь на фото и напиши .wm текст).

Три режима (как у .stik/.ramka):
1. ЛС с ботом: .wm текст → бот просит фото → накладывает знак и возвращает.
   (Если сразу ответить на фото и написать .wm текст — знак наложится сразу.)
2. Бизнес-чат: ответь на фото и напиши .wm текст — бот вернёт фото со знаком.
3. Группа/канал: ответь на фото и напиши .wm текст — фото со знаком в чат.

Как это работает: фото скачивается, по нему плиткой раскладывается
полупрозрачная подпись, повёрнутая на -30° (классическая диагональная сетка —
защита от воровства). Тень под текстом делает его читаемым на любом фоне.
Размер шрифта пропорционален диагонали фото. Шрифт с кириллицей ищется среди
системных (DejaVu / Liberation / Arial / Helvetica / Tahoma); если не найден —
скачивается DejaVuSans.ttf с GitHub raw и кэшируется в temp-файл (тот же
источник, что RAMKA_URL для .ramka). Дефолтный PIL-шрифт — только последний
рубеж, он не знает кириллицу. Обработка идёт в фоновом потоке
(asyncio.to_thread) — бот не зависает.
"""
import asyncio
import os
import tempfile
import threading
import time
import urllib.request
from html import escape as html_escape
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
from PIL import Image, ImageDraw, ImageFont, ImageOps

import database as db
from business_api import (
    _business_delete_message_ex,
    _business_edit_message,
    _get_owner_id_cached,
)
from core import BOT_TOKEN, BOT_USERNAME, S, bot, dp, get_http, log
from functions import LINE

_WM_MAX_CHARS = 64  # дольше не нужно — плитка станет громоздкой

# Системные TTF с кириллицей (поиск по ОС: Linux-сервер, macOS, Windows).
# Если ни одного нет — скачиваем DejaVuSans.ttf (см. _wm_download_font).
_WM_FONT_CANDIDATES = (
    # Linux (Debian/Ubuntu и производные)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    # Windows
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/verdana.ttf",
)
# DejaVuSans — свободный шрифт с полной кириллицей; качаем с GitHub raw
# (тот же источник, что RAMKA_URL у .ramka) и кэшируем в temp-файл.
# Два зеркала на случай недоступности одного из них.
_WM_FONT_URLS = (
    "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/DejaVuSans.ttf",
    "https://raw.githubusercontent.com/matplotlib/matplotlib/main/lib/matplotlib/mpl-data/fonts/ttf/DejaVuSans.ttf",
)

_WM_FONT_DL: Optional[str] = None    # путь к скачанному DejaVuSans.ttf (кэш)
_WM_FONT_DL_FAIL_TS: float = 0.0     # время неудачной попытки — ретрай через 5 мин
_WM_FONT_DL_TTL = 300.0              # как фейл-кэш у _ramka_load_png
_WM_FONT_FALLBACK_LOGGED = False     # предупреждение о шрифте пишем один раз
_WM_FONT_LOCK = threading.Lock()     # рендер идёт в потоках — защищаем загрузку


def _wm_download_font() -> Optional[str]:
    """Скачивает DejaVuSans.ttf (полная кириллица) в temp-файл. None — не вышло."""
    tmp = os.path.join(tempfile.gettempdir(), "quietmod_DejaVuSans.ttf")
    if os.path.exists(tmp) and os.path.getsize(tmp) > 100_000:
        return tmp
    for url in _WM_FONT_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            if not data or len(data) < 100_000:
                log.warning(f"wm: скачанный шрифт подозрительно мал ({len(data) if data else 0} байт)")
                return None
            with open(tmp, "wb") as f:
                f.write(data)
            log.info(f"wm: шрифт DejaVuSans скачан ({len(data) // 1024} КБ)")
            return tmp
        except Exception as e:
            log.warning(f"wm: не удалось скачать шрифт ({e})")
    return None


def _wm_font(size: int):
    """TTF с кириллицей: системный шрифт → скачанный DejaVuSans → дефолтный PIL.

    Дефолтный PIL-шрифт не знает кириллицу (покажет квадратики), поэтому он —
    только последний рубеж, если нет ни системного шрифта, ни интернета.
    """
    global _WM_FONT_DL, _WM_FONT_DL_FAIL_TS, _WM_FONT_FALLBACK_LOGGED
    for p in _WM_FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    with _WM_FONT_LOCK:
        if _WM_FONT_DL is None and time.monotonic() - _WM_FONT_DL_FAIL_TS > _WM_FONT_DL_TTL:
            _WM_FONT_DL = _wm_download_font()
            if _WM_FONT_DL is None:
                _WM_FONT_DL_FAIL_TS = time.monotonic()
    if _WM_FONT_DL:
        try:
            return ImageFont.truetype(_WM_FONT_DL, size)
        except Exception:
            pass
    if not _WM_FONT_FALLBACK_LOGGED:
        _WM_FONT_FALLBACK_LOGGED = True
        log.warning("wm: шрифт с кириллицей недоступен — дефолтный PIL-шрифт покажет квадратики вместо текста")
    try:
        # Pillow >= 10.1 умеет масштабировать встроенный шрифт
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _wm_render(data: bytes, text: str) -> bytes:
    """Фото → фото с диагональным полупрозрачным водяным знаком, JPEG."""
    im = Image.open(BytesIO(data))
    im = ImageOps.exif_transpose(im).convert("RGB")
    if im.width * im.height > 5_000_000:
        im.thumbnail((2200, 2200), Image.LANCZOS)
    w, h = im.size
    diag = (w * w + h * h) ** 0.5

    # размер шрифта пропорционален диагонали фото: читаемо на маленьких,
    # не раздувается в абсурд на огромных
    font = _wm_font(max(14, int(diag * 0.040)))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
    bbox = probe.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # длинный текст уменьшаем, чтобы подпись не вылезала за края картинки
    if tw > 0 and tw > w * 0.8:
        font = _wm_font(max(10, int(font.size * (w * 0.8) / tw)))
        bbox = probe.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if tw <= 0 or th <= 0:
        raise RuntimeError("watermark text is empty")

    # плитка: текст по центру, повёрнут на -30° — классическая сетка
    pad = int(th * 0.9)
    tile_size = max(tw, th) + pad * 2
    tile = Image.new("RGBA", (tile_size, tile_size), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    tx = (tile_size - tw) // 2 - bbox[0]
    ty = (tile_size - th) // 2 - bbox[1]
    td.text((tx + 2, ty + 2), text, font=font, fill=(0, 0, 0, 110))     # тень
    td.text((tx, ty), text, font=font, fill=(255, 255, 255, 130))       # сам знак
    tile = tile.rotate(-30, resample=Image.BICUBIC, expand=True)

    # раскладываем плитки по всей площади (лёгкий нахлёст уплотняет сетку)
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    step_x = max(1, int(tile.width * 0.94))
    step_y = max(1, int(tile.height * 0.94))
    for y in range(-tile.height, h, step_y):
        for x in range(-tile.width, w, step_x):
            overlay.alpha_composite(tile, (x, y))

    out = im.convert("RGBA")
    out.alpha_composite(overlay)
    buf = BytesIO()
    out.convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue()


async def _wm_process_photo(photo: PhotoSize, text: str) -> bytes:
    """Скачивает фото и возвращает JPEG со знаком (обработка в фоновом потоке)."""
    file = await bot.get_file(photo.file_id)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
    session = get_http()
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"download photo: status {resp.status}")
        data = await resp.read()
    return await asyncio.to_thread(_wm_render, data, text)


async def _wm_run(photo: PhotoSize, text: str, chat_id: int,
                  business_connection_id: Optional[str] = None) -> bool:
    """Накладывает знак и отправляет результат в чат. True — успех."""
    try:
        jpg = await _wm_process_photo(photo, text)
    except Exception as e:
        log.error(f"wm process: {e}")
        return False
    try:
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(jpg, filename="watermark.jpg"),
            business_connection_id=business_connection_id,
        )
        return True
    except Exception as e:
        log.error(f"wm send: {e}")
        return False


async def _wm_cleanup(thinking: Optional[Message]) -> None:
    if thinking is None:
        return
    try:
        await thinking.delete()
    except Exception:
        pass


async def _wm_status(photo: PhotoSize, text: str, chat_id: int, send_fn,
                     business_connection_id: Optional[str] = None) -> bool:
    """Статус-сообщение → обработка фото → уборка. False при ошибке (с ответом)."""
    thinking = await send_fn("🏷 Ставлю водяной знак…")
    ok = await _wm_run(photo, text, chat_id, business_connection_id=business_connection_id)
    await _wm_cleanup(thinking)
    if not ok:
        await send_fn("😔 Не получилось наложить знак — попробуй другое фото.")
    return ok


def _wm_reply_photo(msg: Message) -> Optional[PhotoSize]:
    r = msg.reply_to_message
    if r and r.photo:
        return r.photo[-1]
    return None


def _wm_text(msg: Message) -> str:
    """Текст после .wm (с обрезкой до лимита)."""
    raw = (msg.text or msg.caption or "").strip()
    body = raw[3:].strip() if len(raw) >= 3 else ""
    return body[:_WM_MAX_CHARS]


_WM_HINT = (
    f"🏷 <b>.wm</b> — ответь на чьё-то <b>фото</b> и напиши <code>.wm текст</code>,\n"
    f"◇ я наложу полупрозрачную подпись по диагонали.\n\n"
    f"◇ Пример: ответь на фото → <code>.wm не воруй</code>\n"
    f"◇ Лимит: до {_WM_MAX_CHARS} символов\n\n"
    f"— 👁️ @{BOT_USERNAME}"
)


# ── ЛС с ботом: .wm текст → просим фото ───────────────────────────────
@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.wm(\s+.*)?$"), F.chat.type == "private")
async def on_wm_dm(msg: Message, state: FSMContext):
    if not msg.from_user:
        return
    text = _wm_text(msg)
    if not text:
        await msg.answer(_WM_HINT)
        return
    # сразу ответили на фото — делаем без лишних шагов
    reply_photo = _wm_reply_photo(msg)
    if reply_photo:
        await state.clear()
        await _wm_status(reply_photo, text, msg.chat.id, msg.answer)
        return
    await state.set_state(S.wm)
    await state.update_data(text=text)
    await msg.answer(
        f"🏷 <b>ВОДЯНОЙ ЗНАК</b>\n{LINE}\n\n"
        f"◇ Текст: <i>{html_escape(text)}</i>\n\n"
        "◇ Пришли фото — наложу на него знак.\n"
        "◇ Или ответь на чьё-то фото и напиши <code>.wm текст</code> —\n"
        "   так работает и в чатах.\n\n"
        f"— 👁️ @{BOT_USERNAME}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="wm_cancel")]
        ]),
    )


@dp.message(S.wm)
async def on_wm_photo_input(msg: Message, state: FSMContext):
    if not msg.photo:
        await msg.answer("◇ Пришли именно <b>фото</b> — знак умею ставить только на картинки.")
        return
    data = await state.get_data()
    text = data.get("text", "")
    await state.clear()
    await _wm_status(msg.photo[-1], text, msg.chat.id, msg.answer)


@dp.callback_query(F.data == "wm_cancel")
async def cb_wm_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("✕ Отменено", show_alert=False)
    await _wm_cleanup(call.message)


# ── Бизнес-чат: ответь на фото + .wm текст ────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.wm(\s+.*)?$"))
async def on_wm_business(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return
    owner_id = await _get_owner_id_cached(conn_id, ".wm")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    text = _wm_text(msg)
    if not text:
        await _business_edit_message(conn_id, msg.chat.id, msg.message_id, _WM_HINT)
        return
    photo = _wm_reply_photo(msg)
    if not photo:
        await _business_edit_message(conn_id, msg.chat.id, msg.message_id, _WM_HINT)
        return
    ok = await _business_edit_message(conn_id, msg.chat.id, msg.message_id, "🏷 Ставлю водяной знак…")
    if not ok:
        return
    if await _wm_run(photo, text, msg.chat.id, business_connection_id=conn_id):
        try:
            await _business_delete_message_ex(conn_id, msg.message_id)
        except Exception:
            pass
    else:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id,
            "😔 Не получилось наложить знак — попробуй другое фото.",
        )


# ── Группа / канал: ответь на фото + .wm текст ────────────────────────
@dp.message(F.text.regexp(r"(?i)^\.wm(\s+.*)?$"),
            F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_wm_group(msg: Message):
    if not msg.from_user:
        return
    await db.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    text = _wm_text(msg)
    if not text:
        await msg.reply(_WM_HINT)
        return
    photo = _wm_reply_photo(msg)
    if not photo:
        await msg.reply(_WM_HINT)
        return
    await _wm_status(photo, text, msg.chat.id, msg.reply)
