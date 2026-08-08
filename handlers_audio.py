"""🎵 .audio — вытаскивает звук из видео в mp3 (ответь на видео и напиши .audio).

Три режима (как у .krom/.gif):
1. ЛС с ботом: .audio → бот просит видео → достаёт звук и возвращает.
   (Если сразу ответить на видео и написать .audio — mp3 сделается сразу.)
2. Бизнес-чат: ответь на видео и напиши .audio — бот вернёт mp3.
3. Группа/канал: ответь на видео и напиши .audio — mp3 в чат.

Как это работает: видео скачивается, ffmpeg (imageio-ffmpeg, статический
бинарник внутри pip-пакета — ничего ставить на сервер не нужно) отбрасывает
видеодорожку (-vn) и перекодирует аудио в MP3 (libmp3lame, 192 kbps).
Длительность не ограничиваем — в отличие от кружков и гифок, у аудио нет
лимита Telegram на длину. Если файл всё же больше лимита на отправку
(50 МБ) — пережимаем повторно на 96 kbps. Готовый файл отправляется
через send_audio. Если в видео звука нет — ffmpeg вернёт ошибку,
и бот вежливо скажет об этом.
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

# ── Параметры аудио ───────────────────────────────────────────────────
_AUDIO_BITRATE = "192k"        # базовый битрейт (качество как у музыки)
_AUDIO_BITRATE_LOW = "96k"     # повторный пережим: меньше битрейт
_AUDIO_SIZE_LIMIT = 50_000_000  # лимит Telegram на аудио (50 МБ)


def _audio_ffmpeg() -> str:
    """Путь к статическому ffmpeg из imageio-ffmpeg (ставится через pip)."""
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _audio_encode(ffmpeg: str, inp: str, out: str, bitrate: str) -> None:
    """Одна попытка: видео → mp3 (только звук). RuntimeError при ошибке ffmpeg."""
    cmd = [
        ffmpeg, "-y", "-i", inp,
        "-vn",                       # отбрасываем видеодорожку
        "-c:a", "libmp3lame",        # mp3-кодек
        "-b:a", bitrate,
        "-loglevel", "error",
        out,
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {(proc.stderr or b'').decode(errors='replace')[-500:]}")


def _audio_convert(data: bytes) -> bytes:
    """Синхронно (в потоке): видео-байты → mp3 (192k, ≤50 МБ)."""
    ffmpeg = _audio_ffmpeg()
    with tempfile.TemporaryDirectory() as td:
        inp = os.path.join(td, "in.mp4")
        out = os.path.join(td, "out.mp3")
        with open(inp, "wb") as f:
            f.write(data)
        _audio_encode(ffmpeg, inp, out, _AUDIO_BITRATE)
        with open(out, "rb") as f:
            mp3 = f.read()
        # длинные видео могут раздуться за лимит — пережимаем на 96k
        if len(mp3) > _AUDIO_SIZE_LIMIT:
            out2 = os.path.join(td, "out2.mp3")
            try:
                _audio_encode(ffmpeg, inp, out2, _AUDIO_BITRATE_LOW)
                with open(out2, "rb") as f:
                    mp3_2 = f.read()
                if len(mp3_2) < len(mp3):
                    mp3 = mp3_2
            except Exception as e:
                log.warning(f"audio: повторный пережим не удался ({e}) — шлём как есть")
        return mp3


async def _audio_process_video(video: Video) -> bytes:
    """Скачивает видео и возвращает mp3 (конвертация в фоновом потоке)."""
    file = await bot.get_file(video.file_id)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
    session = get_http()
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"download video: status {resp.status}")
        data = await resp.read()
    return await asyncio.to_thread(_audio_convert, data)


def _audio_filename(video: Video) -> str:
    """Имя файла: берём имя исходного видео (если есть), меняем расширение на .mp3."""
    name = getattr(video, "file_name", None) or "audio"
    stem = os.path.splitext(name)[0].strip() or "audio"
    return f"{stem}.mp3"


async def _audio_run(video: Video, chat_id: int,
                     business_connection_id: Optional[str] = None) -> bool:
    """Достаёт звук и отправляет в чат. True — успех."""
    try:
        mp3 = await _audio_process_video(video)
    except Exception as e:
        log.error(f"audio process: {e}")
        return False
    try:
        await bot.send_audio(
            chat_id,
            audio=BufferedInputFile(mp3, filename=_audio_filename(video)),
            business_connection_id=business_connection_id,
        )
        return True
    except Exception as e:
        log.error(f"audio send: {e}")
        return False


async def _audio_cleanup(thinking: Optional[Message]) -> None:
    if thinking is None:
        return
    try:
        await thinking.delete()
    except Exception:
        pass


async def _audio_status(video: Video, chat_id: int, send_fn,
                        business_connection_id: Optional[str] = None) -> bool:
    """Статус-сообщение → обработка видео → уборка. False при ошибке (с ответом)."""
    thinking = await send_fn("◆ · · ·")
    ok = await _audio_run(video, chat_id, business_connection_id=business_connection_id)
    await _audio_cleanup(thinking)
    if not ok:
        await send_fn("◇ Не получилось достать звук — возможно, в видео нет аудиодорожки.")
    return ok


def _audio_reply_video(msg: Message) -> Optional[Video]:
    """Видео из ответа: photo/video_note не считаем, только настоящее видео."""
    r = msg.reply_to_message
    if r and r.video:
        return r.video
    return None


_AUDIO_HINT = (
    f"◇ <b>.audio</b> — ответь на <b>видео</b> и напиши <code>.audio</code>,\n"
    f"◇ я вытащу из него звук и пришлю mp3.\n\n"
    f"— 👁️ @{BOT_USERNAME}"
)


# ── ЛС с ботом: .audio → просим видео ─────────────────────────────────
@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.audio(\s+.*)?$"), F.chat.type == "private")
async def on_audio_dm(msg: Message, state: FSMContext):
    if not msg.from_user:
        return
    # сразу ответили на видео — делаем без лишних шагов
    reply_video = _audio_reply_video(msg)
    if reply_video:
        await state.clear()
        await _audio_status(reply_video, msg.chat.id, msg.answer)
        return
    await state.set_state(S.audio)
    await msg.answer(
        f"◆ <b>АУДИО ИЗ ВИДЕО</b>\n<code>{LINE}</code>\n\n"
        "◇ Пришли видео — вытащу из него звук.\n"
        "◇ Или ответь на чьё-то видео и напиши <code>.audio</code> —\n"
        "   так работает и в чатах.\n\n"
        f"— 👁️ @{BOT_USERNAME}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="audio_cancel")]
        ]),
    )


@dp.message(S.audio)
async def on_audio_video_input(msg: Message, state: FSMContext):
    if not msg.video:
        await msg.answer("◇ Пришли именно <b>видео</b> — из фото или текста звук не вытащу.")
        return
    await state.clear()
    await _audio_status(msg.video, msg.chat.id, msg.answer)


@dp.callback_query(F.data == "audio_cancel")
async def cb_audio_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("✕ Отменено", show_alert=False)
    await _audio_cleanup(call.message)


# ── Бизнес-чат: ответь на видео + .audio ──────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.audio(\s+.*)?$"))
async def on_audio_business(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return
    owner_id = await _get_owner_id_cached(conn_id, ".audio")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    video = _audio_reply_video(msg)
    if not video:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id, _AUDIO_HINT,
        )
        return
    ok = await _business_edit_message(conn_id, msg.chat.id, msg.message_id, "◆ · · ·")
    if not ok:
        return
    if await _audio_run(video, msg.chat.id, business_connection_id=conn_id):
        try:
            await _business_delete_message_ex(conn_id, msg.message_id)
        except Exception:
            pass
    else:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id,
            "◇ Не получилось достать звук — возможно, в видео нет аудиодорожки.",
        )


# ── Группа / канал: ответь на видео + .audio ──────────────────────────
@dp.message(F.text.regexp(r"(?i)^\.audio(\s+.*)?$"),
            F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_audio_group(msg: Message):
    if not msg.from_user:
        return
    await db.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    video = _audio_reply_video(msg)
    if not video:
        await msg.reply(_AUDIO_HINT)
        return
    await _audio_status(video, msg.chat.id, msg.reply)
