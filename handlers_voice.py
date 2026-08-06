"""🎙 .voice — озвучивает текст голосовым сообщением (ИИ-голос, бесплатно).

Три режима (как у .ramka/.stik/.krom):
1. ЛС с ботом: .voice текст → голосовое сообщение.
2. Бизнес-чат: .voice текст → голосовое в чат.
3. Группа/канал: .voice текст → голосовое в чат.

Как это работает: текст синтезируется нейроголосом Microsoft Edge (edge-tts —
бесплатно, без API-ключей; голос ru-RU-SvetlanaNeural), MP3 пережимается в
нативный OGG/Opus через ffmpeg (imageio-ffmpeg, статический бинарник внутри
pip-пакета — ничего ставить на сервер не нужно). Так Telegram принимает файл
как настоящее голосовое сообщение. Если Opus недоступен — отправляем MP3
(Telegram пережмёт сам). Длинный текст обрезается до ~60 секунд озвучки —
лимит голосовых.
"""
import asyncio
import os
import subprocess
import tempfile
from io import BytesIO
from typing import Optional

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.types import BufferedInputFile, Message

import edge_tts

import database as db
from business_api import (
    _business_delete_message_ex,
    _business_edit_message,
    _get_owner_id_cached,
)
from core import BOT_USERNAME, bot, dp, log

# ── Параметры озвучки ─────────────────────────────────────────────────
_VOICE_NAME = "ru-RU-SvetlanaNeural"   # нейроголос: Светлана (женский, Microsoft Edge)
_VOICE_MAX_CHARS = 800                 # ~60 секунд озвучки (лимит голосовых)


def _voice_tts_sync(text: str) -> bytes:
    """Синхронно (в потоке): текст → MP3-байты через edge-tts."""
    async def _run() -> bytes:
        communicate = edge_tts.Communicate(text, _VOICE_NAME)
        buf = BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        data = buf.getvalue()
        if not data:
            raise RuntimeError("edge-tts: пустой аудиопоток")
        return data
    return asyncio.run(_run())


def _voice_ogg_sync(mp3: bytes) -> Optional[bytes]:
    """MP3 → OGG/Opus — нативный формат голосовых. None → шлём MP3 как есть."""
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        log.warning(f"voice: ffmpeg недоступен ({e}) — шлём MP3")
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            inp = os.path.join(td, "in.mp3")
            out = os.path.join(td, "out.ogg")
            with open(inp, "wb") as f:
                f.write(mp3)
            cmd = [
                ffmpeg, "-y", "-i", inp,
                "-c:a", "libopus", "-b:a", "32k", "-ar", "48000",
                "-loglevel", "error",
                out,
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
            if proc.returncode != 0:
                log.warning("voice: opus-конвертация не удалась — шлём MP3")
                return None
            with open(out, "rb") as f:
                return f.read()
    except Exception as e:
        log.warning(f"voice: ogg-конвертация ({e}) — шлём MP3")
        return None


async def _voice_render(text: str) -> tuple[Optional[bytes], str]:
    """Текст → (аудио-байты, имя файла). Тяжёлая работа в фоновом потоке."""
    try:
        mp3 = await asyncio.to_thread(_voice_tts_sync, text)
    except Exception as e:
        log.error(f"voice tts: {e}")
        return None, ""
    ogg = await asyncio.to_thread(_voice_ogg_sync, mp3)
    if ogg:
        return ogg, "voice.ogg"
    return mp3, "voice.mp3"


async def _voice_run(text: str, chat_id: int,
                     business_connection_id: Optional[str] = None) -> bool:
    """Озвучивает текст и отправляет голосовое в чат. True — успех."""
    data, filename = await _voice_render(text)
    if not data:
        return False
    try:
        await bot.send_voice(
            chat_id,
            voice=BufferedInputFile(data, filename=filename),
            business_connection_id=business_connection_id,
        )
        return True
    except Exception as e:
        log.error(f"voice send: {e}")
        return False


async def _voice_cleanup(thinking: Optional[Message]) -> None:
    if thinking is None:
        return
    try:
        await thinking.delete()
    except Exception:
        pass


async def _voice_status(text: str, chat_id: int, send_fn,
                        business_connection_id: Optional[str] = None) -> bool:
    """Статус-сообщение → озвучка → уборка. False при ошибке (с ответом)."""
    thinking = await send_fn("🎙 Озвучиваю…")
    ok = await _voice_run(text, chat_id, business_connection_id=business_connection_id)
    await _voice_cleanup(thinking)
    if not ok:
        await send_fn("😔 Не получилось озвучить — попробуй ещё раз.")
    return ok


def _voice_body(msg: Message) -> str:
    """Текст после .voice (с обрезкой до лимита голосовых)."""
    raw = (msg.text or msg.caption or "").strip()
    body = raw[6:].strip() if len(raw) >= 6 else ""
    if not body:
        return ""
    if len(body) > _VOICE_MAX_CHARS:
        body = body[:_VOICE_MAX_CHARS].rstrip() + "…"
    return body


_VOICE_HINT = (
    f"🎙 <b>.voice</b> — напиши <code>.voice текст</code>,\n"
    f"◇ я озвучу его голосовым сообщением (ИИ-голос).\n\n"
    f"◇ Пример: <code>.voice привет, как дела?</code>\n"
    f"◇ Лимит: до {_VOICE_MAX_CHARS} символов (~60 сек)\n\n"
    f"— 👁️ @{BOT_USERNAME}"
)


# ── ЛС с ботом: .voice текст ──────────────────────────────────────────
@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.voice(\s+.*)?$"), F.chat.type == "private")
async def on_voice_dm(msg: Message):
    if not msg.from_user:
        return
    text = _voice_body(msg)
    if not text:
        await msg.answer(_VOICE_HINT)
        return
    await _voice_status(text, msg.chat.id, msg.answer)


# ── Бизнес-чат: .voice текст ──────────────────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.voice(\s+.*)?$"))
async def on_voice_business(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return
    owner_id = await _get_owner_id_cached(conn_id, ".voice")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    text = _voice_body(msg)
    if not text:
        await _business_edit_message(conn_id, msg.chat.id, msg.message_id, _VOICE_HINT)
        return
    ok = await _business_edit_message(conn_id, msg.chat.id, msg.message_id, "🎙 Озвучиваю…")
    if not ok:
        return
    if await _voice_run(text, msg.chat.id, business_connection_id=conn_id):
        try:
            await _business_delete_message_ex(conn_id, msg.message_id)
        except Exception:
            pass
    else:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id,
            "😔 Не получилось озвучить — попробуй ещё раз.",
        )


# ── Группа / канал: .voice текст ──────────────────────────────────────
@dp.message(F.text.regexp(r"(?i)^\.voice(\s+.*)?$"),
            F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_voice_group(msg: Message):
    if not msg.from_user:
        return
    await db.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    text = _voice_body(msg)
    if not text:
        await msg.reply(_VOICE_HINT)
        return
    await _voice_status(text, msg.chat.id, msg.reply)
