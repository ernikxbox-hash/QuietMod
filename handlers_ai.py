"""ИИ-консьерж в ЛС с ботом и поиск по архиву."""
import asyncio

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import database as db
from core import S, bot, dp, log
from functions import LINE, _get_image_base64, _reply_ai_html, _send_code_files, ai_history, groq_chat, home_text, kb_ai, kb_back, kb_main


@dp.callback_query(F.data == "ai_open")
async def cb_ai_open(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    AI_ACTIVE_USERS.add(uid)
    await state.set_state(S.ai_chat)
    await call.answer()
    await call.message.edit_text(
        f"◆ <b>ИИ-консьерж</b>\n{LINE}\n"
        f"Модель: <b>Llama 3.3 70B</b>\n"
        f"Лимит: <b>без ограничений</b>\n\n"
        "Спрашивай что угодно — отвечу тихо и быстро ◆",
        reply_markup=kb_ai(),
    )

THINKING_FRAMES = ["◜ 👁️ Думаю", "◝ 👁️ Думаю", "◞ 👁️ Думаю", "◟ 👁️ Думаю"]
THINKING_INTERVAL = 0.4
AI_ACTIVE_USERS: set[int] = set()


def _should_handle_ai_message(current_state, uid: int, ai_active_ids: set[int]) -> bool:
    if uid in ai_active_ids:
        return True
    if current_state in {S.ai_chat, "ai_chat", getattr(S.ai_chat, "state", None)}:
        return True
    return False


async def _spin_thinking(chat_id: int, message_id: int):
    i = 0
    try:
        while True:
            frame = THINKING_FRAMES[i % len(THINKING_FRAMES)]
            try:
                await bot.edit_message_text(frame, chat_id=chat_id, message_id=message_id)
            except Exception:
                pass
            i += 1
            await asyncio.sleep(THINKING_INTERVAL)
    except asyncio.CancelledError:
        pass

@dp.message(F.text | F.photo, F.chat.type.in_({"private"}))
async def ai_msg(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    current_state = await state.get_state()
    if not _should_handle_ai_message(current_state, uid, AI_ACTIVE_USERS):
        return
    has_photo = bool(msg.photo)
    has_text  = bool(msg.text or msg.caption)
    if not has_text and not has_photo:
        await msg.answer("◇ Отправь текст или фото (можно с подписью).")
        return
    text_content = (msg.text or msg.caption or "").strip()
    if has_photo and not text_content:
        text_content = "Опиши что на фото и ответь кратко и по делу."
    elif has_photo and text_content:
        text_content = f"{text_content}\n\nОпиши что на фото и ответь кратко и по делу."
    thinking = await msg.answer(THINKING_FRAMES[0])
    spin_task = asyncio.create_task(_spin_thinking(thinking.chat.id, thinking.message_id))
    image_b64 = None
    if has_photo:
        file_id = msg.photo[-1].file_id
        image_b64 = await _get_image_base64(bot, file_id)
        if image_b64 is None:
            spin_task.cancel()
            await thinking.edit_text("◇ Не смог загрузить фото — попробуй ещё раз.")
            return
    try:
        reply, files = await groq_chat(uid, text_content, image_base64=image_b64)
    finally:
        spin_task.cancel()
    await thinking.delete()
    await _reply_ai_html(msg, prefix="◆ ", answer=reply, reply_markup=kb_ai())
    if files:
        await _send_code_files(uid, files)

@dp.callback_query(F.data == "ai_clear")
async def cb_ai_clear(call: CallbackQuery):
    ai_history.pop(call.from_user.id, None)
    await call.answer("✕ Диалог сброшен", show_alert=True)

@dp.callback_query(F.data == "ai_exit")
async def cb_ai_exit(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    AI_ACTIVE_USERS.discard(uid)
    await call.answer()
    await call.message.edit_text(
        home_text(),
        reply_markup=kb_main(uid),
    )

@dp.callback_query(F.data == "search")
async def cb_search(call: CallbackQuery, state: FSMContext):
    await state.set_state(S.ai_search)
    await call.answer()
    await call.message.edit_text(
        f"◐ <b>Поиск по архиву</b>\n{LINE}\n"
        "Введи имя, @username или ключевое слово:",
        reply_markup=kb_back("menu"),
    )

@dp.message(S.ai_search)
async def search_msg(msg: Message, state: FSMContext):
    if not msg.text:
        return
    await state.clear()
    uid     = msg.from_user.id
    results = await db.search_messages(uid, msg.text.strip())
    if not results:
        await msg.answer(
            f"◐ <b>Ничего не найдено</b> по «{msg.text}»",
            reply_markup=kb_back("menu"),
        )
        return
    lines = []
    for m in results[:15]:
        preview = (m["text"][:40] + "…") if len(m["text"] or "") > 40 else (m["text"] or m["media_type"])
        lines.append(f"◆ <b>{m['from_name']}</b>  {m['date']}\n   {preview}")
    await msg.answer(
        f"◐ <b>Найдено: {len(results)}</b>\n{LINE}\n" + "\n\n".join(lines),
        reply_markup=kb_back("menu"),
    )
