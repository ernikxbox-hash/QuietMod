"""🎥 .krom — конвертирует видео в кружок (ответь на видео и напиши .krom).

Три режима (как у .ramka/.stik):
1. ЛС с ботом: .krom → бот просит видео → превращает в кружок и возвращает.
   (Если сразу ответить на видео и написать .krom — кружок сделается сразу.)
2. Бизнес-чат: ответь на видео и напиши .krom — бот вернёт кружок.
3. Группа/канал: ответь на видео и напиши .krom — кружок в чат.

Как это работает: видео скачивается, ffmpeg (imageio-ffmpeg, статический
бинарник внутри pip-пакета — ничего ставить на сервер не нужно) обрезает
кадр до квадрата 640×640 по центру и перекодирует в MP4/H.264 (как
настоящий кружок Telegram). Длинные видео обрезаются до 60 секунд —
лимит кружков. Готовый файл отправляется через send_video_note.
"""
import asyncio
import os
import subprocess
import tempfile
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
    Video,
)

import database as db
from business_api import (
    _business_delete_message_ex,
    _business_edit_message,
    _get_owner_id_cached,
)
from core import BOT_TOKEN, BOT_USERNAME, S, bot, dp, get_http, log
from functions import LINE

# ── Параметры кружка Telegram ─────────────────────────────────────────
_KROM_SIZE = 640          # сторона квадрата (кружки — квадратные 640×640)
_KROM_MAX_SEC = 60        # максимальная длительность кружка


def _krom_ffmpeg() -> str:
    """Путь к статическому ffmpeg из imageio-ffmpeg (ставится через pip)."""
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _krom_convert(data: bytes) -> bytes:
    """Синхронно (в потоке): видео-байты → кружок (MP4/H.264 640×640 ≤60с)."""
    ffmpeg = _krom_ffmpeg()
    with tempfile.TemporaryDirectory() as td:
        inp = os.path.join(td, "in.mp4")
        out = os.path.join(td, "out.mp4")
        with open(inp, "wb") as f:
            f.write(data)
        # -vf: сначала масштабируем, перекрывая 640×640, потом режем 640×640
        # по центру — классический cover-fit для кружка. -t 60 — лимит длины.
        cmd = [
            ffmpeg, "-y", "-i", inp,
            "-vf", f"scale={_KROM_SIZE}:{_KROM_SIZE}:force_original_aspect_ratio=increase,crop={_KROM_SIZE}:{_KROM_SIZE}",
            "-t", str(_KROM_MAX_SEC),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-pix_fmt", "yuv420p",          # совместимость со всеми плеерами (HDR/10bit-исходники)
            "-c:a", "aac", "-b:a", "96k",  # кружок со звуком, как настоящий (native aac)
            "-movflags", "+faststart",
            "-loglevel", "error",
            out,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg: {(proc.stderr or b'').decode(errors='replace')[-500:]}")
        with open(out, "rb") as f:
            return f.read()


async def _krom_process_video(video: Video) -> bytes:
    """Скачивает видео и возвращает кружок (конвертация в фоновом потоке)."""
    file = await bot.get_file(video.file_id)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
    session = get_http()
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"download video: status {resp.status}")
        data = await resp.read()
    return await asyncio.to_thread(_krom_convert, data)


async def _krom_run(video: Video, chat_id: int,
                    business_connection_id: Optional[str] = None) -> bool:
    """Делает кружок и отправляет в чат. True — успех."""
    try:
        mp4 = await _krom_process_video(video)
    except Exception as e:
        log.error(f"krom process: {e}")
        return False
    try:
        await bot.send_video_note(
            chat_id,
            video_note=BufferedInputFile(mp4, filename="video_note.mp4"),
            business_connection_id=business_connection_id,
        )
        return True
    except Exception as e:
        log.error(f"krom send: {e}")
        return False


async def _krom_cleanup(thinking: Optional[Message]) -> None:
    if thinking is None:
        return
    try:
        await thinking.delete()
    except Exception:
        pass


async def _krom_status(video: Video, chat_id: int, send_fn,
                       business_connection_id: Optional[str] = None) -> bool:
    """Статус-сообщение → обработка видео → уборка. False при ошибке (с ответом)."""
    thinking = await send_fn("🎥 Делаю кружок…")
    ok = await _krom_run(video, chat_id, business_connection_id=business_connection_id)
    await _krom_cleanup(thinking)
    if not ok:
        await send_fn("😔 Не получилось сделать кружок — попробуй другое видео.")
    return ok


def _krom_reply_video(msg: Message) -> Optional[Video]:
    """Видео из ответа: photo/video_note не считаем, только настоящее видео."""
    r = msg.reply_to_message
    if r and r.video:
        return r.video
    return None


_KROM_HINT = (
    f"🎥 <b>.krom</b> — ответь на <b>видео</b> и напиши <code>.krom</code>,\n"
    f"◇ я превращу его в кружок.\n\n"
    f"— 👁️ @{BOT_USERNAME}"
)


# ── ЛС с ботом: .krom → просим видео ──────────────────────────────────
@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.krom(\s+.*)?$"), F.chat.type == "private")
async def on_krom_dm(msg: Message, state: FSMContext):
    if not msg.from_user:
        return
    # сразу ответили на видео — делаем без лишних шагов
    reply_video = _krom_reply_video(msg)
    if reply_video:
        await state.clear()
        await _krom_status(reply_video, msg.chat.id, msg.answer)
        return
    await state.set_state(S.krom)
    await msg.answer(
        f"🎥 <b>КРУЖОК ИЗ ВИДЕО</b>\n{LINE}\n\n"
        "◇ Пришли видео — сделаю из него кружок.\n"
        "◇ Или ответь на чьё-то видео и напиши <code>.krom</code> —\n"
        "   так работает и в чатах.\n\n"
        f"— 👁️ @{BOT_USERNAME}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="krom_cancel")]
        ]),
    )


@dp.message(S.krom)
async def on_krom_video_input(msg: Message, state: FSMContext):
    if not msg.video:
        await msg.answer("◇ Пришли именно <b>видео</b> — из фото или текста кружок не сделаю.")
        return
    await state.clear()
    await _krom_status(msg.video, msg.chat.id, msg.answer)


@dp.callback_query(F.data == "krom_cancel")
async def cb_krom_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("✕ Отменено", show_alert=False)
    await _krom_cleanup(call.message)


# ── Бизнес-чат: ответь на видео + .krom ───────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.krom(\s+.*)?$"))
async def on_krom_business(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return
    owner_id = await _get_owner_id_cached(conn_id, ".krom")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    video = _krom_reply_video(msg)
    if not video:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id, _KROM_HINT,
        )
        return
    ok = await _business_edit_message(conn_id, msg.chat.id, msg.message_id, "🎥 Делаю кружок…")
    if not ok:
        return
    if await _krom_run(video, msg.chat.id, business_connection_id=conn_id):
        try:
            await _business_delete_message_ex(conn_id, msg.message_id)
        except Exception:
            pass
    else:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id,
            "😔 Не получилось сделать кружок — попробуй другое видео.",
        )


# ── Группа / канал: ответь на видео + .krom ───────────────────────────
@dp.message(F.text.regexp(r"(?i)^\.krom(\s+.*)?$"),
            F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_krom_group(msg: Message):
    if not msg.from_user:
        return
    await db.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    video = _krom_reply_video(msg)
    if not video:
        await msg.reply(_KROM_HINT)
        return
    await _krom_status(video, msg.chat.id, msg.reply)
