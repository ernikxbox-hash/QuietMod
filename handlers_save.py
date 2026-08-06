"""⏬ .save — скачивает видео по ссылке (TikTok, YouTube, Instagram, …).

Три режима (как у .ramka/.stik/.krom):
1. ЛС с ботом: .save → бот просит ссылку → скачивает и присылает видео.
   (Если сразу написать .save https://… — скачает сразу.)
2. Бизнес-чат: напиши .save https://… — бот вернёт видео.
3. Группа/канал: .save https://… — видео в чат.

Движок — yt-dlp (умеет сотни сайтов; по умолчанию разрешены TikTok,
YouTube и Instagram). Ограничения:
- только ссылки на разрешённые домены (защита от SSRF);
- видео не больше 50 МБ (лимит Bot API);
- без плейлистов — скачивается только один ролик по ссылке;
- одно скачивание на пользователя одновременно (не даёт спамить).
"""
import asyncio
import os
import re
import tempfile
from typing import Optional
from urllib.parse import urlparse

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import database as db
from business_api import (
    _business_delete_message_ex,
    _business_edit_message,
    _get_owner_id_cached,
)
from core import BOT_USERNAME, S, bot, dp, log
from functions import LINE

# ── Параметры скачивания ──────────────────────────────────────────────
_SAVE_MAX_BYTES = 50 * 1024 * 1024        # лимит Bot API на загрузку файлов
_SAVE_ALLOWED_HOSTS = (
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "instagram.com",
    "instagr.am",
)
_SAVE_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_save_busy: set[int] = set()              # пользователи, у которых идёт скачивание

# cookies.txt (Netscape format) для платформ, требующих авторизации
# (в основном Instagram). Путь к файлу — env SAVE_COOKIES_FILE.
_SAVE_COOKIES_FILE = os.environ.get("SAVE_COOKIES_FILE", "").strip()


def _save_ffmpeg() -> str:
    """Путь к статическому ffmpeg из imageio-ffmpeg (для склейки потоков)."""
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _save_extract_url(text: str) -> Optional[str]:
    """Первая ссылка в тексте, ведущая на разрешённый домен."""
    for m in _SAVE_URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;:!?)]}")
        url = url.rstrip("'\"")
        host = (urlparse(url).hostname or "").lower()
        if any(host == d or host.endswith("." + d) for d in _SAVE_ALLOWED_HOSTS):
            return url
    return None


def _save_download(url: str) -> tuple[bytes, str]:
    """Синхронно (в потоке): ссылка → (байты видео, имя файла).

    yt-dlp с ffmpeg из imageio-ffmpeg: если у видео отдельные потоки
    видео/аудио (часто на YouTube) — они склеиваются в один MP4.
    """
    import yt_dlp
    ffmpeg_dir = os.path.dirname(_save_ffmpeg())
    with tempfile.TemporaryDirectory() as td:
        outtmpl = os.path.join(td, "%(title).80s [%(id)s].%(ext)s")
        opts: dict = {
            # до 1080p: если ссылка на 4K/8K, берём лучший поток ≤1080p,
            # иначе max_filesize оборвёт всё скачивание без шанса на файл
            "format": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
            "merge_output_format": "mp4",
            "outtmpl": outtmpl,
            "noplaylist": True,            # только один ролик, не весь плейлист
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "socket_timeout": 30,
            "retries": 2,
            "ffmpeg_location": ffmpeg_dir,
            "max_filesize": _SAVE_MAX_BYTES,  # yt-dlp сам бросит, если больше лимита
        }
        if _SAVE_COOKIES_FILE and os.path.isfile(_SAVE_COOKIES_FILE):
            opts["cookiefile"] = _SAVE_COOKIES_FILE
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = os.listdir(td)
        # yt-dlp может оставить .part / .ytdl — берём только готовые файлы
        ready = [f for f in files if not f.endswith((".part", ".ytdl"))]
        if not ready:
            raise RuntimeError("yt-dlp: файл не создан")
        path = os.path.join(td, max(ready, key=lambda f: os.path.getsize(os.path.join(td, f))))
        with open(path, "rb") as f:
            data = f.read()
        return data, os.path.basename(path)


async def _save_run(url: str, chat_id: int, uid: int,
                    business_connection_id: Optional[str] = None) -> bool:
    """Скачивает видео и отправляет в чат. True — успех."""
    if uid in _save_busy:
        return False
    _save_busy.add(uid)
    try:
        try:
            data, fname = await asyncio.wait_for(
                asyncio.to_thread(_save_download, url), timeout=240
            )
        except asyncio.TimeoutError:
            log.warning(f"save timeout uid={uid} url={url[:60]}")
            return False
        except Exception as e:
            log.error(f"save download: {e}")
            return False
        try:
            await bot.send_video(
                chat_id,
                video=BufferedInputFile(data, filename=fname),
                business_connection_id=business_connection_id,
            )
            return True
        except Exception as e:
            log.error(f"save send video: {e}")
            # видео не прошло как video (например, поток без видео-дорожки
            # в mp4) — пробуем отправить документом, чтобы не терять файл
            try:
                await bot.send_document(
                    chat_id,
                    document=BufferedInputFile(data, filename=fname),
                    business_connection_id=business_connection_id,
                )
                return True
            except Exception as e2:
                log.error(f"save send document: {e2}")
                return False
    finally:
        _save_busy.discard(uid)


async def _save_cleanup(thinking: Optional[Message]) -> None:
    if thinking is None:
        return
    try:
        await thinking.delete()
    except Exception:
        pass


async def _save_status(url: str, chat_id: int, uid: int, send_fn,
                       business_connection_id: Optional[str] = None) -> bool:
    """Статус-сообщение → скачивание → уборка. False при ошибке (с ответом)."""
    if uid in _save_busy:
        await send_fn("⏳ Уже скачиваю другое видео — дождись, потом пробуй снова.")
        return False
    thinking = await send_fn("⏬ Скачиваю видео…")
    ok = await _save_run(url, chat_id, uid, business_connection_id=business_connection_id)
    await _save_cleanup(thinking)
    if not ok:
        await send_fn(
            "😔 Не получилось скачать — проверь ссылку, или видео больше 50 МБ, "
            "или платформа не отдаёт его без авторизации."
        )
    return ok


_SAVE_HINT = (
    f"⏬ <b>.save</b> — пришли ссылку на видео, я скачаю его:\n"
    f"◇ TikTok · YouTube · Instagram.\n\n"
    f"◇ Пример: <code>.save https://youtu.be/…</code>\n\n"
    f"— 👁️ @{BOT_USERNAME}"
)


# ── ЛС с ботом: .save → просим ссылку ─────────────────────────────────
@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.save(\s+.*)?$"), F.chat.type == "private")
async def on_save_dm(msg: Message, state: FSMContext):
    if not msg.from_user:
        return
    url = _save_extract_url(msg.text or "")
    if url:
        await state.clear()
        await _save_status(url, msg.chat.id, msg.from_user.id, msg.answer)
        return
    await state.set_state(S.save)
    await msg.answer(
        f"⏬ <b>СКАЧАТЬ ВИДЕО</b>\n{LINE}\n\n"
        "◇ Пришли ссылку на видео — скачаю.\n"
        "◇ Поддерживаю: <b>TikTok</b>, <b>YouTube</b>, <b>Instagram</b>.\n\n"
        f"— 👁️ @{BOT_USERNAME}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="save_cancel")]
        ]),
    )


@dp.message(S.save)
async def on_save_link_input(msg: Message, state: FSMContext):
    if not msg.from_user:
        return
    url = _save_extract_url(msg.text or "")
    if not url:
        await msg.answer("◇ Это не похоже на ссылку на TikTok / YouTube / Instagram.")
        return
    await state.clear()
    await _save_status(url, msg.chat.id, msg.from_user.id, msg.answer)


@dp.callback_query(F.data == "save_cancel")
async def cb_save_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("✕ Отменено", show_alert=False)
    await _save_cleanup(call.message)


# ── Бизнес-чат: .save ссылка ──────────────────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.save(\s+.*)?$"))
async def on_save_business(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return
    owner_id = await _get_owner_id_cached(conn_id, ".save")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    url = _save_extract_url(msg.text or "")
    if not url:
        await _business_edit_message(conn_id, msg.chat.id, msg.message_id, _SAVE_HINT)
        return
    if owner_id in _save_busy:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id,
            "⏳ Уже скачиваю другое видео — дождись, потом пробуй снова.",
        )
        return
    ok = await _business_edit_message(conn_id, msg.chat.id, msg.message_id, "⏬ Скачиваю видео…")
    if not ok:
        return
    if await _save_run(url, msg.chat.id, owner_id, business_connection_id=conn_id):
        try:
            await _business_delete_message_ex(conn_id, msg.message_id)
        except Exception:
            pass
    else:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id,
            "😔 Не получилось скачать — проверь ссылку, или видео больше 50 МБ, "
            "или платформа не отдаёт его без авторизации.",
        )


# ── Группа / канал: .save ссылка ──────────────────────────────────────
@dp.message(F.text.regexp(r"(?i)^\.save(\s+.*)?$"),
            F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_save_group(msg: Message):
    if not msg.from_user:
        return
    await db.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    url = _save_extract_url(msg.text or "")
    if not url:
        await msg.reply(_SAVE_HINT)
        return
    await _save_status(url, msg.chat.id, msg.from_user.id, msg.reply)
