"""🎞 .gif — конвертирует видео в анимированную гифку (ответь на видео и напиши .gif).

Три режима (как у .ramka/.stik/.krom):
1. ЛС с ботом: .gif → бот просит видео → делает гифку и возвращает.
   (Если сразу ответить на видео и написать .gif — гифка сделается сразу.)
2. Бизнес-чат: ответь на видео и напиши .gif — бот вернёт гифку.
3. Группа/канал: ответь на видео и напиши .gif — гифка в чат.

Как это работает: видео скачивается, ffmpeg (imageio-ffmpeg, статический
бинарник внутри pip-пакета — ничего ставить на сервер не нужно) пережимает
его в GIF с палитрой: 2-проходной palettegen/paletteuse + bayer-дизеринг —
максимум качества при минимуме размера. Кадр вписан в квадрат 480×480 с
сохранением пропорций, частота 12 fps, длительность ограничена 15 секундами
(длинные гифки раздуваются в десятки мегабайт). Если гифка всё равно больше
лимита Telegram (8 МБ) — пережимаем повторно с меньшими fps и размером.
Готовый файл отправляется через send_animation.
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

# ── Параметры гифки ───────────────────────────────────────────────────
_GIF_MAX_SEC = 15        # лимит длительности (гифки быстро раздуваются)
_GIF_FPS = 12            # базовая частота кадров
_GIF_MAX_SIZE = 480      # вписываем в 480×480 с сохранением пропорций
_GIF_SIZE_LIMIT = 8_000_000   # лимит Telegram на анимацию (8 МБ)
_GIF_FPS_LOW = 8         # повторный пережим: меньше fps
_GIF_SIZE_LOW = 360      # ... и меньше размер


def _gif_ffmpeg() -> str:
    """Путь к статическому ffmpeg из imageio-ffmpeg (ставится через pip)."""
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _gif_vf(fps: int, max_size: int) -> str:
    """Фильтр-граф: кадры → палитра → гифка (2-проходный palettegen/paletteuse)."""
    return (
        f"fps={fps},"
        f"scale={max_size}:{max_size}:force_original_aspect_ratio=decrease,"
        f"split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5"
    )


def _gif_encode(ffmpeg: str, inp: str, out: str, fps: int, max_size: int) -> None:
    """Одна попытка конвертации видео → GIF. RuntimeError при ошибке ffmpeg."""
    cmd = [
        ffmpeg, "-y", "-i", inp,
        "-t", str(_GIF_MAX_SEC),
        "-vf", _gif_vf(fps, max_size),
        "-loop", "0",
        "-loglevel", "error",
        out,
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {(proc.stderr or b'').decode(errors='replace')[-500:]}")


def _gif_convert(data: bytes) -> bytes:
    """Синхронно (в потоке): видео-байты → GIF (≤15с, 12fps, ≤480px)."""
    ffmpeg = _gif_ffmpeg()
    with tempfile.TemporaryDirectory() as td:
        inp = os.path.join(td, "in.mp4")
        out = os.path.join(td, "out.gif")
        with open(inp, "wb") as f:
            f.write(data)
        _gif_encode(ffmpeg, inp, out, _GIF_FPS, _GIF_MAX_SIZE)
        with open(out, "rb") as f:
            gif = f.read()
        # гифки быстро раздуваются — если вышли за лимит Telegram, пережимаем
        if len(gif) > _GIF_SIZE_LIMIT:
            out2 = os.path.join(td, "out2.gif")
            try:
                _gif_encode(ffmpeg, out, out2, _GIF_FPS_LOW, _GIF_SIZE_LOW)
                with open(out2, "rb") as f:
                    gif2 = f.read()
                if len(gif2) < len(gif):
                    gif = gif2
            except Exception as e:
                log.warning(f"gif: повторный пережим не удался ({e}) — шлём как есть")
        return gif


async def _gif_process_video(video: Video) -> bytes:
    """Скачивает видео и возвращает гифку (конвертация в фоновом потоке)."""
    file = await bot.get_file(video.file_id)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
    session = get_http()
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"download video: status {resp.status}")
        data = await resp.read()
    return await asyncio.to_thread(_gif_convert, data)


async def _gif_run(video: Video, chat_id: int,
                   business_connection_id: Optional[str] = None) -> bool:
    """Делает гифку и отправляет в чат. True — успех."""
    try:
        gif = await _gif_process_video(video)
    except Exception as e:
        log.error(f"gif process: {e}")
        return False
    try:
        await bot.send_animation(
            chat_id,
            animation=BufferedInputFile(gif, filename="animation.gif"),
            business_connection_id=business_connection_id,
        )
        return True
    except Exception as e:
        log.error(f"gif send: {e}")
        return False


async def _gif_cleanup(thinking: Optional[Message]) -> None:
    if thinking is None:
        return
    try:
        await thinking.delete()
    except Exception:
        pass


async def _gif_status(video: Video, chat_id: int, send_fn,
                      business_connection_id: Optional[str] = None) -> bool:
    """Статус-сообщение → обработка видео → уборка. False при ошибке (с ответом)."""
    thinking = await send_fn("◆ · · ·")
    ok = await _gif_run(video, chat_id, business_connection_id=business_connection_id)
    await _gif_cleanup(thinking)
    if not ok:
        await send_fn("◇ Не получилось сделать гифку — попробуй другое видео.")
    return ok


def _gif_reply_video(msg: Message) -> Optional[Video]:
    """Видео из ответа: photo/video_note не считаем, только настоящее видео."""
    r = msg.reply_to_message
    if r and r.video:
        return r.video
    return None


_GIF_HINT = (
    f"◇ <b>.gif</b> — ответь на <b>видео</b> и напиши <code>.gif</code>,\n"
    f"◇ я превращу его в анимированную гифку (до {_GIF_MAX_SEC} сек).\n\n"
    f"— 👁️ @{BOT_USERNAME}"
)


# ── ЛС с ботом: .gif → просим видео ───────────────────────────────────
@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.gif(\s+.*)?$"), F.chat.type == "private")
async def on_gif_dm(msg: Message, state: FSMContext):
    if not msg.from_user:
        return
    # сразу ответили на видео — делаем без лишних шагов
    reply_video = _gif_reply_video(msg)
    if reply_video:
        await state.clear()
        await _gif_status(reply_video, msg.chat.id, msg.answer)
        return
    await state.set_state(S.gif)
    await msg.answer(
        f"◆ <b>ГИФКА ИЗ ВИДЕО</b>\n<code>{LINE}</code>\n\n"
        "◇ Пришли видео — сделаю из него гифку.\n"
        "◇ Или ответь на чьё-то видео и напиши <code>.gif</code> —\n"
        "   так работает и в чатах.\n\n"
        f"— 👁️ @{BOT_USERNAME}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="gif_cancel")]
        ]),
    )


@dp.message(S.gif)
async def on_gif_video_input(msg: Message, state: FSMContext):
    if not msg.video:
        await msg.answer("◇ Пришли именно <b>видео</b> — из фото или текста гифку не сделаю.")
        return
    await state.clear()
    await _gif_status(msg.video, msg.chat.id, msg.answer)


@dp.callback_query(F.data == "gif_cancel")
async def cb_gif_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("✕ Отменено", show_alert=False)
    await _gif_cleanup(call.message)


# ── Бизнес-чат: ответь на видео + .gif ────────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.gif(\s+.*)?$"))
async def on_gif_business(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return
    owner_id = await _get_owner_id_cached(conn_id, ".gif")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    video = _gif_reply_video(msg)
    if not video:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id, _GIF_HINT,
        )
        return
    ok = await _business_edit_message(conn_id, msg.chat.id, msg.message_id, "◆ · · ·")
    if not ok:
        return
    if await _gif_run(video, msg.chat.id, business_connection_id=conn_id):
        try:
            await _business_delete_message_ex(conn_id, msg.message_id)
        except Exception:
            pass
    else:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id,
            "◇ Не получилось сделать гифку — попробуй другое видео.",
        )


# ── Группа / канал: ответь на видео + .gif ────────────────────────────
@dp.message(F.text.regexp(r"(?i)^\.gif(\s+.*)?$"),
            F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_gif_group(msg: Message):
    if not msg.from_user:
        return
    await db.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    video = _gif_reply_video(msg)
    if not video:
        await msg.reply(_GIF_HINT)
        return
    await _gif_status(video, msg.chat.id, msg.reply)
