"""Админка (/admin), .cmd список функций, рассылки и перехват групповых сообщений."""
import asyncio
from datetime import datetime, timedelta, timezone

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from html import escape as html_escape

import database as db
from business_api import _business_edit_message, _business_send_message_ex
from core import ADMIN_ID, CHANNEL_USERNAME, GROQ_API_KEYS, S, bot, dp, log
from handlers_gate import set_channel_id
from functions import (
    LINE,
    CMD_FEATURES,
    MSK,
    _edit_ai_html,
    kb_admin,
    kb_back,
    kb_cmd,
)
from handlers_games import _knb_cache_member
from handlers_intercept import (
    _cache_transcript,
    _transcribe_voice,
    _tsc_kb,
    _tsc_teaser,
)


def _is_admin(call: CallbackQuery) -> bool:
    return call.from_user.id == ADMIN_ID


@dp.callback_query(F.data == "adm")
async def cb_adm(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call):
        await call.answer("⛔", show_alert=True)
        return
    await state.clear()
    await call.answer()
    await call.message.edit_text(
        f"▲ <b>Admin Suite</b>\n{LINE}",
        reply_markup=kb_admin(),
    )


USERS_PAGE_SIZE = 10


def _fmt_user_line(u: dict) -> str:
    uname = f"@{u['username']}" if u.get("username") else (u.get("full_name") or "—")
    if u.get("referrer_id"):
        source = f"⟡ по приглашению (от ID {u['referrer_id']})"
    else:
        source = "◇ по юзернейму / прямой запуск"
    return f"<b>{html_escape(uname)}</b>  (ID {u['id']})\n   {source}"


async def _render_users_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_users()
    offset = page * USERS_PAGE_SIZE
    users = await db.get_all_users(limit=USERS_PAGE_SIZE, offset=offset)
    if not users:
        text = f"◆ <b>Пользователи</b>\n{LINE}\nВсего: <b>{total}</b>\n\nПусто."
    else:
        lines = [_fmt_user_line(u) for u in users]
        page_count = (total + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE
        text = (
            f"◆ <b>Пользователи</b>  ({total})\n{LINE}\n\n"
            + "\n\n".join(lines)
            + f"\n\n{LINE}\nСтраница {page + 1} / {max(page_count, 1)}"
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"adm_users_p{page-1}"))
    if offset + USERS_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Вперёд →", callback_data=f"adm_users_p{page+1}"))
    rows = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="← В меню", callback_data="adm")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adm_users")
async def cb_adm_users(call: CallbackQuery):
    if not _is_admin(call): return
    await call.answer()
    text, kb = await _render_users_page(0)
    await call.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("adm_users_p"))
async def cb_adm_users_page(call: CallbackQuery):
    if not _is_admin(call): return
    page = int(call.data.removeprefix("adm_users_p"))
    await call.answer()
    text, kb = await _render_users_page(page)
    await call.message.edit_text(text, reply_markup=kb)


def _stat_since(days: int = 0) -> str:
    """Начало периода в UTC (naive ISO): days=0 — с начала сегодняшнего дня по МСК."""
    now_msk = datetime.now(MSK)
    start = now_msk.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    return start.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


async def _render_stats() -> tuple[str, InlineKeyboardMarkup]:
    """Текст статистики + клавиатура (обновить / сбросить ошибки ключей)."""
    today = _stat_since(0)
    week  = _stat_since(6)
    month = _stat_since(29)
    n_keys = len(GROQ_API_KEYS)
    # Все запросы статистики — параллельно (gather): раньше 11+ последовательных
    # запросов к БД держали админку ~1 сек, теперь всё летит разом.
    results = await asyncio.gather(
        db.count_stats("launch", today),
        db.count_stats("launch", week),
        db.count_stats("launch", month),
        db.count_stats("launch"),
        db.count_users(),
        db.total_messages_all(),
        db.total_stars(),
        db.count_ideas(),
        db.count_stats("caught_deleted"),
        db.count_stats("caught_edited"),
        db.count_stats("whisper_ok"),
        *[db.count_stats(f"groq_key{i}_ok") for i in range(1, n_keys + 1)],
        *[db.count_stats(f"groq_key{i}_ok", today) for i in range(1, n_keys + 1)],
        *[db.count_stats(f"groq_key{i}_fail") for i in range(1, n_keys + 1)],
    )
    (l_today, l_week, l_month, l_all,
     users, msgs, stars, ideas,
     del_caught, ed_caught, whisper) = results[:11]
    key_ok_a   = results[11:11 + n_keys]
    key_ok_t   = results[11 + n_keys:11 + 2 * n_keys]
    key_fail_a = results[11 + 2 * n_keys:11 + 3 * n_keys]
    launches = (l_today, l_week, l_month, l_all)
    key_lines = []
    ok_total = 0
    for i in range(n_keys):
        ok_a   = key_ok_a[i]
        ok_t   = key_ok_t[i]
        fail_a = key_fail_a[i]
        ok_total += ok_a
        key_lines.append(
            f"   🔑 Ключ {i + 1}: <b>{ok_a}</b> ok (сегодня {ok_t}) · ошибок <b>{fail_a}</b>"
        )
    if not key_lines:
        key_lines = ["   — ключи не настроены —"]
    text = (
        f"◆ <b>Статистика бота</b>\n{LINE}\n"
        f"🚀 Запуски:  сегодня <b>{launches[0]}</b> · 7д <b>{launches[1]}</b> · "
        f"30д <b>{launches[2]}</b> · всего <b>{launches[3]}</b>\n"
        f"{LINE}\n"
        f"🤖 <b>Groq API</b> (успешных ответов: <b>{ok_total}</b>)\n"
        + "\n".join(key_lines)
        + f"\n{LINE}\n"
        f"✕ Перехвачено удалённых:   <b>{del_caught}</b>\n"
        f"✦ Перехвачено изменённых:  <b>{ed_caught}</b>\n"
        f"🎤 Расшифровок голосовых:  <b>{whisper}</b>\n"
        f"{LINE}\n"
        f"◇ Пользователей:  <b>{users}</b>\n"
        f"◇ Записей в БД:   <b>{msgs}</b>\n"
        f"⟡ Собрано звёзд:  <b>{stars}</b>\n"
        f"✦ Предложений:    <b>{ideas}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⟳ Обновить", callback_data="adm_stats"),
            InlineKeyboardButton(text="🔄 Сброс ошибок ключей", callback_data="adm_stats_reset_fails"),
        ],
        [InlineKeyboardButton(text="← Назад", callback_data="adm")],
    ])
    return text, kb


@dp.callback_query(F.data == "adm_stats")
async def cb_adm_stats(call: CallbackQuery):
    if not _is_admin(call): return
    await call.answer()
    text, kb = await _render_stats()
    await call.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data == "adm_stats_reset_fails")
async def cb_adm_stats_reset_fails(call: CallbackQuery):
    """Сброс ошибок всех API-ключей: дальше в статистике видно только новые."""
    if not _is_admin(call): return
    n1 = await db.delete_stats_like("groq_key%_fail")
    n2 = await db.delete_stats_like("whisper_fail")
    await call.answer(f"🔄 Ошибки сброшены · удалено: {n1 + n2}", show_alert=False)
    text, kb = await _render_stats()
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        log.error(f"stats reset render: {e}")


@dp.callback_query(F.data == "adm_ideas")
async def cb_adm_ideas(call: CallbackQuery):
    if not _is_admin(call): return
    await call.answer()
    ideas = await db.get_ideas(30)
    if not ideas:
        await call.message.edit_text(
            f"✦ <b>Предложения от пользователей</b>\n{LINE}\n"
            "Пока пусто — расскажи людям о кнопке.",
            reply_markup=kb_admin(),
        )
        return
    lines = []
    for idea in ideas[:10]:
        uname = f"@{idea['username']}" if idea['username'] else idea['full_name']
        preview = idea['text'][:80] + ("…" if len(idea['text']) > 80 else "")
        lines.append(
            f"<b>#{idea['id']}</b> · {uname}\n"
            f"   {html_escape(preview)}"
        )
    kb_rows = []
    for idea in ideas[:10]:
        kb_rows.append([InlineKeyboardButton(
            text=f"✕ Удалить #{idea['id']}",
            callback_data=f"adm_del_idea_{idea['id']}"
        )])
    kb_rows.append([InlineKeyboardButton(text="✕ Очистить все", callback_data="adm_clear_ideas")])
    kb_rows.append([InlineKeyboardButton(text="← Назад", callback_data="adm")])
    await call.message.edit_text(
        f"✦ <b>Предложения от пользователей</b>  ({len(ideas)} шт.)\n{LINE}\n\n"
        + "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


@dp.callback_query(F.data.startswith("adm_del_idea_"))
async def cb_adm_del_idea(call: CallbackQuery):
    if not _is_admin(call): return
    idea_id = int(call.data.split("_")[-1])
    await db.delete_idea(idea_id)
    await call.answer(f"✕ Предложение #{idea_id} удалено")
    await cb_adm_ideas(call)


@dp.callback_query(F.data == "adm_clear_ideas")
async def cb_adm_clear_ideas(call: CallbackQuery):
    if not _is_admin(call): return
    await db.clear_ideas()
    await call.answer("✕ Все предложения очищены", show_alert=True)
    await call.message.edit_text(
        f"✦ <b>Предложения от пользователей</b>\n{LINE}\n"
        "Список очищен.",
        reply_markup=kb_admin(),
    )


@dp.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call): return
    await call.answer()
    await state.set_state(S.broadcast)
    await call.message.edit_text(
        f"▤ <b>Сообщение всем пользователям</b>\n{LINE}\n\n"
        "Отправь сообщение, которое получат <b>все</b>,\n"
        "кто хоть раз писал /start боту.\n\n"
        "Поддерживаются текст, фото, видео и другие медиа\n"
        "с подписью — формат сохранится.\n\n"
        "✕ Для отмены — нажми кнопку ниже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="adm")]
        ]),
    )


@dp.message(S.broadcast)
async def on_broadcast_input(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        await state.clear()
        return
    await state.clear()
    ids = await db.all_user_ids()
    status = await msg.answer(f"▤ Рассылка начата · 0 / {len(ids)}…")
    ok = 0
    fail = 0
    for i, uid in enumerate(ids, start=1):
        try:
            await msg.copy_to(chat_id=uid)
            ok += 1
        except Exception as e:
            fail += 1
            log.warning(f"broadcast to {uid}: {e}")
        await asyncio.sleep(0.05)
        if i % 25 == 0 or i == len(ids):
            try:
                await status.edit_text(f"▤ Рассылка идёт · {i} / {len(ids)}…")
            except Exception:
                pass
    await status.edit_text(
        f"▤ <b>Рассылка завершена</b>\n{LINE}\n"
        f"✔ Доставлено: <b>{ok}</b>\n"
        f"✕ Не доставлено: <b>{fail}</b>",
        reply_markup=kb_admin(),
    )


@dp.callback_query(F.data == "suggest_idea")
async def cb_suggest_idea(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(S.suggest_idea)
    await call.message.edit_text(
        f"✦ <b>Предложить идею</b>\n{LINE}\n\n"
        "Расскажи, что бы ты хотел видеть в боте.\n"
        "Любая идея — полезная функция, улучшение\n"
        "интерфейса, новая команда — всё приветствуется.\n\n"
        "◇ Напиши своё предложение:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="back_menu")]
        ]),
    )


@dp.message(S.suggest_idea)
async def on_idea_input(msg: Message, state: FSMContext):
    uid   = msg.from_user.id
    text  = msg.text or msg.caption or ""
    if not text.strip():
        await msg.answer("◇ Напиши текст идеи — пустое сообщение не принято.")
        return
    await state.clear()
    await db.save_idea(
        uid,
        msg.from_user.username or "",
        msg.from_user.full_name or "",
        text.strip()
    )
    await msg.answer(
        f"✦ <b>Спасибо за идею!</b>\n{LINE}\n\n"
        "Твоё предложение отправлено разработчику.\n"
        "Лучшие идеи попадают в следующие обновления.\n\n"
        "Ты помогаешь сделать Quiet Mod лучше.",
        reply_markup=kb_back("menu"),
    )
    uname = f"@{msg.from_user.username}" if msg.from_user.username else msg.from_user.full_name
    try:
        await bot.send_message(
            ADMIN_ID,
            f"✦ <b>Новая идея!</b>\n{LINE}\n"
            f"◇ {uname} (ID: {uid})\n\n"
            f"◇ {html_escape(text[:500])}",
        )
    except Exception:
        pass


@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):
    chat = update.chat
    new_status = update.new_chat_member.status
    is_gate_channel = (
        chat.type == "channel"
        and CHANNEL_USERNAME
        and (getattr(chat, "username", "") or "").lower() == CHANNEL_USERNAME.lower()
    )
    if new_status in ("member", "administrator", "restricted"):
        was_added = new_status != "restricted" or update.old_chat_member.status in ("left", "kicked")
        if was_added or new_status == "administrator" or new_status == "member":
            await db.add_bot_chat(chat.id, chat.title or chat.full_name or "", chat.type)
            if new_status == "restricted":
                log.info(f"📌 Бот ограничен в {chat.type} «{chat.title or chat.full_name or chat.id}» (ID: {chat.id}) — оставлен в списке")
            else:
                log.info(f"📌 Бот добавлен в {chat.type} «{chat.title or chat.full_name or chat.id}» (ID: {chat.id})")
            # 🛡 Бота добавили именно в канал гейта — фиксируем ID сразу
            if is_gate_channel:
                set_channel_id(chat.id)
                log.info(f"🛡 Гейт: это канал @{CHANNEL_USERNAME} — ID зафиксирован, подписка заработала")
    elif new_status in ("left", "kicked"):
        await db.remove_bot_chat(chat.id)
        log.info(f"📌 Бот удалён из {chat.type} «{chat.title or chat.full_name or chat.id}» (ID: {chat.id})")
        if is_gate_channel:
            log.warning(f"🛡 Гейт: бота удалили из канала @{CHANNEL_USERNAME} — доступ временно открыт!")


@dp.callback_query(F.data == "adm_broadcast_groups")
async def cb_adm_broadcast_groups(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call):
        return
    await call.answer()
    chats = await db.get_all_bot_chats()
    if not chats:
        await call.message.edit_text(
            f"▤ <b>Рассылка по группам/каналам</b>\n{LINE}\n\n"
            "Бот пока не добавлен ни в одну группу или канал.\n\n"
            "Добавь бота в группу/канал и выдай права\n"
            "администратора — после этого чат появится в\n"
            "списке и сюда можно будет делать рассылку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="← Назад", callback_data="adm")]
            ]),
        )
        return
    await state.set_state(S.broadcast_groups)
    chat_list = "\n".join(
        f"◇ {c['title'] or '—'} ({c['chat_type']}, ID: {c['id']})"
        for c in chats
    )
    await call.message.edit_text(
        f"▤ <b>Рассылка по группам/каналам</b>\n{LINE}\n\n"
        f"Бот админ в <b>{len(chats)}</b> чатах:\n"
        f"{chat_list}\n\n"
        f"{LINE}\n"
        "Отправь сообщение — оно будет скопировано\n"
        "во все чаты, где бот администратор.\n\n"
        "Поддерживаются текст, фото, видео и другие\n"
        "медиа с подписью — формат сохранится.\n\n"
        "✕ Для отмены — нажми кнопку ниже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="adm")]
        ]),
    )


@dp.message(F.text.regexp(r"(?i)^\.cmd$"), F.chat.type.in_({"private", "group", "supergroup", "channel"}))
async def on_cmd(msg: Message):
    await msg.answer(
        f"◆ <b>QUIET MOD</b> 👁️ — список команд\n{LINE}\n\n"
        "Выбери команду:",
        reply_markup=kb_cmd(),
    )


@dp.business_message(F.text.regexp(r"(?i)^\.cmd$"))
async def on_cmd_business(msg: Message):
    await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        f"◆ <b>QUIET MOD</b> 👁️ — список команд"
    )
    await _business_send_message_ex(
        msg.business_connection_id, msg.chat.id,
        f"◆ <b>QUIET MOD</b> 👁️ — список команд\n{LINE}\n\n"
        "Выбери команду:"
    )


@dp.callback_query(F.data.startswith("cmd_info_"))
async def cb_cmd_info(call: CallbackQuery):
    key = call.data.replace("cmd_info_", "")
    feat = CMD_FEATURES.get(key)
    if not feat:
        await call.answer("Функция не найдена", show_alert=True)
        return
    text = (
        f"{feat['title']}\n{LINE}\n\n"
        f"{feat['desc']}\n\n"
        f"<b>Использование:</b>\n<code>{feat['usage']}</code>\n\n"
        f"<b>Пример:</b>\n<code>{feat['example']}</code>\n\n"
        f"◇ {feat['note']}"
    )
    await call.answer()
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← К списку", callback_data="cmd_back")],
        [InlineKeyboardButton(text="✕ Закрыть", callback_data="cmd_close")],
    ]))


@dp.callback_query(F.data == "cmd_back")
async def cb_cmd_back(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"◆ <b>QUIET MOD</b> 👁️ — список функций\n{LINE}\n\n"
        "Выбери интересующую функцию:",
        reply_markup=kb_cmd(),
    )


@dp.callback_query(F.data == "cmd_close")
async def cb_cmd_close(call: CallbackQuery):
    await call.answer("✕ Закрыто")
    try:
        await call.message.delete()
    except Exception:
        pass


@dp.message(F.text.regexp(r"(?i)^\.emoji(\s+\S+)?$"), F.chat.type == "private")
async def on_emoji_cmd(msg: Message):
    """Админ: список кастомных эмодзи пака с их custom_emoji_id (.emoji <pack>).

    Пример: .emoji CPT_Emoji — бот покажет все эмодзи пака, у каждого
    кастомного эмодзи есть цифровой custom_emoji_id. Его можно ставить
    на кнопки (icon_custom_emoji_id) и в текст сообщений.
    """
    if not msg.from_user or msg.from_user.id != ADMIN_ID:
        return
    body = msg.text.strip().split(maxsplit=1)
    if len(body) < 2:
        await msg.answer("◇ Формат: <code>.emoji имя_пака</code>\n◇ Пример: <code>.emoji CPT_Emoji</code>")
        return
    pack = body[1].strip().lstrip("@")
    try:
        sticker_set = await bot.get_sticker_set(pack)
    except Exception as e:
        log.info(f"👁 .emoji pack not found: {pack} ({e})")
        await msg.answer(
            f"◇ <b>Не нашёл пак</b> «{html_escape(pack)}».\n"
            "◇ Проверь имя: <code>.emoji CPT_Emoji</code>\n"
            "   (без t.me/addemoji/)"
        )
        return
    lines = []
    for s in sticker_set.stickers:
        eid = s.custom_emoji_id or ""
        lines.append(f"{s.emoji or '❔'} → <code>{eid}</code>")
    text = (
        f"👁 <b>Пак {html_escape(sticker_set.name)}</b> · {len(sticker_set.stickers)} эмодзи\n"
        f"{LINE}\n\n"
        + "\n".join(lines)
    )
    if len(text) > 4000:
        text = text[:3970] + "\n…"
    await msg.answer(text)
    log.info(f"👁 .emoji pack={pack} count={len(sticker_set.stickers)}")


@dp.message(S.broadcast_groups)
async def on_broadcast_groups_input(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        await state.clear()
        return
    await state.clear()
    chats = await db.get_all_bot_chats()
    if not chats:
        await msg.answer(
            "▤ Нет чатов для рассылки — бот нигде не админ.",
            reply_markup=kb_admin(),
        )
        return
    status = await msg.answer(f"▤ Рассылка по группам/каналам · 0 / {len(chats)}…")
    ok = 0
    fail = 0
    removed = 0
    for i, chat in enumerate(chats, start=1):
        try:
            await msg.copy_to(chat_id=chat["id"])
            ok += 1
        except Exception as e:
            err_str = str(e).lower()
            if "group chat was upgraded" in err_str or "chat not found" in err_str or "migrated" in err_str:
                await db.remove_bot_chat(chat["id"])
                removed += 1
                log.info(f"🧹 Удалён устаревший чат {chat['id']} ({chat.get('title', '?')}) из БД")
            else:
                fail += 1
                log.warning(f"broadcast_groups to {chat['id']} ({chat.get('title', '?')}): {e}")
        await asyncio.sleep(0.05)
        if i % 10 == 0 or i == len(chats):
            try:
                await status.edit_text(f"▤ Рассылка по группам/каналам · {i} / {len(chats)}…")
            except Exception:
                pass
    result_parts = [f"✔ Доставлено: <b>{ok}</b>"]
    if fail:
        result_parts.append(f"✕ Ошибок: <b>{fail}</b>")
    if removed:
        result_parts.append(f"🧹 Устаревших чатов удалено: <b>{removed}</b>")
    await status.edit_text(
        f"▤ <b>Рассылка по группам/каналам завершена</b>\n{LINE}\n" + "\n".join(result_parts),
        reply_markup=kb_admin(),
    )


@dp.message(F.chat.type.in_({"group", "supergroup", "channel"}))
@dp.channel_post()
async def on_group_msg(msg: Message):
    """Сохраняет чат в БД при любом сообщении в группе/канале."""
    if msg.chat.type in ("group", "supergroup", "channel"):
        await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
        if msg.from_user:
            await db.upsert_user(
                msg.from_user.id,
                msg.from_user.username or "",
                msg.from_user.full_name or "",
            )
            _knb_cache_member(msg.chat.id, msg.from_user)
    if msg.voice or msg.video_note:
        media_label = "голосового" if msg.voice else "кружка"
        try:
            thinking = await msg.reply("🎤 Расшифровываю…")
        except Exception as e:
            log.error(f"group voice thinking reply: {e}")
            return
        try:
            file_id = (msg.voice or msg.video_note).file_id
            transcript = await _transcribe_voice(file_id)
            if transcript:
                token = _cache_transcript(media_label, transcript)
                try:
                    await thinking.edit_text(
                        _tsc_teaser(media_label),
                        reply_markup=_tsc_kb(token),
                    )
                except Exception as e:
                    log.error(f"group voice teaser edit: {e}")
            else:
                await _edit_ai_html(
                    thinking,
                    prefix="",
                    answer="😔 <b>Не удалось расшифровать</b> — попробуй ещё раз.",
                )
        except Exception as e:
            log.error(f"group voice/video transcription: {e}")
            try:
                await _edit_ai_html(
                    thinking,
                    prefix="",
                    answer="😔 <b>Не удалось расшифровать</b> — попробуй ещё раз.",
                )
            except Exception:
                try:
                    await thinking.delete()
                except Exception:
                    pass
