"""Ядро бота: перехват удалённых и изменённых сообщений + расшифровка голосовых."""
import asyncio
import os
from typing import Optional

import aiohttp
from aiogram import F
from aiogram.types import (
    BusinessMessagesDeleted,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from html import escape as html_escape

import database as db
from business_api import (
    _business_delete_message_ex,
    _business_edit_message,
    _business_send_message_ex,
    _get_owner_id_cached,
)
from core import BOT_TOKEN, BOT_USERNAME, GROQ_API_KEYS, bot, dp, get_http, log
from functions import (
    LINE,
    MEDIA_MAP,
    _send_notify,
    _wbl_should_delete,
    business_afk,
    business_afk_last_reply,
    business_code_mode,
    business_muted_chats,
    business_nomute_chats,
    business_wbl_chats,
    fmt_msg_date,
    fmt_sender,
    kb_notify,
    user_afk,
)
from handlers_games import _knb_cache_member


def _extract_media(msg: Message) -> tuple[str, Optional[str]]:
    """Первый медиа-объект сообщения: (подпись из MEDIA_MAP, file_id)."""
    for attr, label in MEDIA_MAP.items():
        obj = getattr(msg, attr, None)
        if obj:
            file_id = obj[-1].file_id if attr == "photo" else (getattr(obj, "file_id", None))
            return label, file_id
    return "◆ Текст", None


@dp.business_message()
async def on_business_msg(msg: Message):
    if not msg.business_connection_id:
        return
    if msg.text and msg.text.lower().startswith((".ai ", ".spam ", ".price", ".curs", ".mute", ".unmute", ".nomute", ".unnomute", ".afk", ".unafk", ".code", ".uncode", ".wbl", ".unwbl", ".cmd", ".knb", ".ramka", ".stik", ".krom", ".info", ".status", ".bold ", ".italic ", ".mono ", ".line ", ".crossed ", ".hidden ", ".quote ")):
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, "save")
    if owner_id is None:
        return
    if msg.from_user and getattr(msg.chat, "type", None) in ("group", "supergroup"):
        _knb_cache_member(msg.chat.id, msg.from_user)
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
        msg.text
        and msg.from_user
        and msg.from_user.id == owner_id
        and getattr(msg.chat, "type", None) == "private"
        and (msg.business_connection_id, msg.chat.id) in business_nomute_chats
        and not msg.text.startswith(".")
    ):
        ok, retry_after, _ = await _business_send_message_ex(msg.business_connection_id, msg.chat.id, msg.text)
        if not ok and retry_after:
            await asyncio.sleep(int(retry_after))
            await _business_send_message_ex(msg.business_connection_id, msg.chat.id, msg.text)
        log.info(f"🛡️ nomute resend conn={msg.business_connection_id} chat={msg.chat.id} owner={owner_id}")
    if (
        getattr(msg.chat, "type", None) == "private"
        and msg.from_user
        and msg.from_user.id != owner_id
        and (msg.business_connection_id, msg.chat.id) in business_wbl_chats
        and _wbl_should_delete((msg.text or msg.caption or ""))
    ):
        ok, retry_after, _ = await _business_delete_message_ex(msg.business_connection_id, msg.message_id)
        if not ok and retry_after:
            await asyncio.sleep(int(retry_after))
            await _business_delete_message_ex(msg.business_connection_id, msg.message_id)
        log.debug(f"🧹 wbl deleted msg={msg.message_id} owner={owner_id}")
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
    afk = business_afk.get(msg.business_connection_id) or user_afk.get(owner_id)
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
            parts = ["Я сейчас не в сети."]
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
    media_type, file_id = _extract_media(msg)
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
    log.debug(f"📥 cached msg={msg.message_id} owner={owner_id}")

@dp.edited_business_message()
async def on_edited_business_msg(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, "edit")
    if owner_id is None:
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
        await db.record_stat("caught_edited")
        await _send_notify(owner_id, notify, reply_markup=kb_notify(save_id))
    media_type, file_id = _extract_media(msg)
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
    log.debug(f"✏️ updated msg={msg.message_id} owner={owner_id} bot_edit={is_bot_edit}")


# ── Расшифровка голосовых (Whisper) ───────────────────────────────────
_WHISPER_FILE_MAP = {
    ".oga": ("voice.ogg", "audio/ogg"),
    ".ogg": ("voice.ogg", "audio/ogg"),
    ".mp4": ("video_note.mp4", "video/mp4"),
}

# ── Сворачивание расшифровок (кнопка «Тык») ──────────────────
_TRANSCRIPT_MAX_MSG     = 4000   # безопасный лимит Telegram на сообщение
_TRANSCRIPT_CACHE: dict[str, dict] = {}
_TRANSCRIPT_CACHE_MAX  = 300

def _chunk_text(text: str, size: int = _TRANSCRIPT_MAX_MSG) -> list[str]:
    """Режет текст на куски по границе строк, не разрывая HTML-сущности."""
    if len(text) <= size:
        return [text]
    chunks = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        while cut < len(text) and "&" in text[max(0, cut - 12):cut] and not text[cut - 1] == ";":
            cut += 1
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks

def _cache_transcript(label: str, text: str) -> str:
    token = os.urandom(6).hex()
    _TRANSCRIPT_CACHE[token] = {"label": label, "text": text, "chunks": []}
    if len(_TRANSCRIPT_CACHE) > _TRANSCRIPT_CACHE_MAX:
        stale = list(_TRANSCRIPT_CACHE)[:len(_TRANSCRIPT_CACHE) - _TRANSCRIPT_CACHE_MAX]
        for k in stale:
            _TRANSCRIPT_CACHE.pop(k, None)
    return token

def _tsc_teaser(label: str) -> str:
    return f"🎤 <b>Расшифровка {label}:</b>\n{LINE}\n👆 <b>Тык</b> — откроется полный текст"

def _tsc_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👆 Тык", callback_data=f"show_tsc_{token}")],
    ])

async def _transcribe_voice(file_id: str) -> Optional[str]:
    try:
        file = await bot.get_file(file_id)
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
        session = get_http()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                return None
            audio_bytes = await resp.read()
        import io
        ext = os.path.splitext(file.file_path or "")[1].lower()
        fname, content_type = _WHISPER_FILE_MAP.get(ext, ("voice.ogg", "audio/ogg"))
        for api_key in GROQ_API_KEYS:
            try:
                form = aiohttp.FormData()
                form.add_field("file", io.BytesIO(audio_bytes), filename=fname, content_type=content_type)
                form.add_field("model", "whisper-large-v3")
                form.add_field("response_format", "text")
                session = get_http()
                async with session.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        await db.record_stat("whisper_ok")
                        return (await resp.text()).strip()
                    _whisper_err = (await resp.text())[:300]
                    log.warning(
                        f"Whisper key failed (status={resp.status}, body={_whisper_err}) — пробую следующий ключ"
                    )
            except Exception as e:
                log.warning(f"Whisper transcribe (key attempt): {e}")
        await db.record_stat("whisper_fail")
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
    log.debug(f"🚨 deleted conn={event.business_connection_id} ids={event.message_ids}")
    owner_id = await _get_owner_id_cached(event.business_connection_id, "delete")
    if owner_id is None:
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
        await db.record_stat("caught_deleted")
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

@dp.callback_query(F.data.startswith("show_tsc_"))
async def cb_show_transcript(call: CallbackQuery):
    token = call.data[len("show_tsc_"):]
    await call.answer()
    if not call.message:
        return
    entry = _TRANSCRIPT_CACHE.get(token)
    if not entry:
        try:
            await call.message.edit_text("😔 Текст расшифровки больше недоступен.")
        except Exception:
            pass
        return
    label = entry["label"]
    full = f"🎤 <b>Расшифровка {label}:</b>\n{LINE}\n{html_escape(entry['text'])}"
    hide_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔼 Скрыть", callback_data=f"hide_tsc_{token}")],
    ])
    if len(full) <= _TRANSCRIPT_MAX_MSG:
        entry["chunks"] = []
        try:
            await call.message.edit_text(full, reply_markup=hide_kb)
        except Exception as e:
            log.error(f"show transcript edit: {e}")
        return
    entry["chunks"] = []
    parts = _chunk_text(html_escape(entry["text"]), _TRANSCRIPT_MAX_MSG - 60)
    try:
        await call.message.edit_text(
            f"🎤 <b>Расшифровка {label}:</b>\n{LINE}\n{parts[0]}",
            reply_markup=hide_kb,
        )
    except Exception as e:
        log.error(f"show transcript edit (long): {e}")
    for part in parts[1:]:
        try:
            sent = await bot.send_message(call.message.chat.id, part)
            entry["chunks"].append(sent.message_id)
        except Exception:
            try:
                sent = await bot.send_message(call.message.chat.id, part, parse_mode=None)
                entry["chunks"].append(sent.message_id)
            except Exception as e:
                log.error(f"show transcript chunk: {e}")

@dp.callback_query(F.data.startswith("hide_tsc_"))
async def cb_hide_transcript(call: CallbackQuery):
    token = call.data[len("hide_tsc_"):]
    await call.answer()
    if not call.message:
        return
    entry = _TRANSCRIPT_CACHE.get(token)
    label = (entry or {}).get("label", "голосового")
    for mid in (entry or {}).get("chunks", []):
        try:
            await bot.delete_message(call.message.chat.id, mid)
        except Exception:
            pass
    if entry:
        entry["chunks"] = []
    try:
        await call.message.edit_text(
            _tsc_teaser(label),
            reply_markup=_tsc_kb(token),
        )
    except Exception as e:
        log.error(f"hide transcript: {e}")
