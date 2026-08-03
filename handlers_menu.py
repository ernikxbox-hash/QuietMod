"""ЛС-меню: сохранённые, подключение, приглашения, профиль, архив, донаты."""
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from html import escape as html_escape

import database as db
from core import ADMIN_ID, BOT_USERNAME, bot, dp, log
from functions import (
    LINE,
    fmt_sender,
    home_msg,
    home_text,
    kb_back,
    kb_donate,
    kb_main,
    ref_link,
)
from handlers_intercept import _send_media


@dp.callback_query(F.data.startswith("save_"))
async def cb_save_forever(call: CallbackQuery):
    msg_id = int(call.data.split("_")[1])
    uid = call.from_user.id
    cached = await db.get_message(uid, msg_id)
    if not cached:
        await call.answer("✕ Сообщение не найдено в кэше", show_alert=True)
        return
    sender = fmt_sender(cached["from_name"], cached["username"])
    save_text = (
        f"◆ <b>Сохранено из перехвата</b>\n"
        f"{LINE}\n"
        f"◇ От: <b>{sender}</b>\n"
        f"◆ Чат: {cached['chat']}\n"
        f"◷ Время: {cached['date']}\n"
        f"◇ Тип: {cached['media_type']}"
    )
    if cached["text"]:
        save_text += f"\n{LINE}\n◆ {html_escape(cached['text'])}"
    try:
        await bot.send_message(uid, save_text)
        if cached["file_id"]:
            await _send_media(uid, cached["file_id"], cached["media_type"])
        await call.answer("◆ Сохранено в архиве!", show_alert=False)
        new_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✔ Принято",      callback_data=f"ack_{msg_id}"),
                InlineKeyboardButton(text="✕ Стереть",      callback_data=f"del_{msg_id}"),
            ],
            [InlineKeyboardButton(text="◆ Сохранено",        callback_data="noop")],
            [InlineKeyboardButton(text="▣ Весь архив",       callback_data="show_all")],
        ])
        await call.message.edit_reply_markup(reply_markup=new_kb)
    except Exception as e:
        log.error(f"save_forever: {e}")

@dp.callback_query(F.data.startswith("back_"))
async def cb_back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    await call.answer()
    await call.message.edit_text(
        home_text(),
        reply_markup=kb_main(uid),
    )

@dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()

@dp.callback_query(F.data.startswith("nsave_"))
async def cb_notify_save(call: CallbackQuery):
    save_id = int(call.data.split("_")[1])
    uid     = call.from_user.id
    await call.answer("◆ Сохранено на 7 дней", show_alert=False)
    try:
        await call.message.delete()
    except Exception:
        pass
    existing_id = home_msg.get(uid)
    if existing_id:
        try:
            await bot.edit_message_text(
                home_text(), chat_id=uid, message_id=existing_id,
                reply_markup=kb_main(uid), parse_mode="HTML"
            )
            return
        except Exception:
            pass
    sent = await bot.send_message(uid, home_text(), reply_markup=kb_main(uid))
    home_msg[uid] = sent.message_id

@dp.callback_query(F.data.startswith("ndel_"))
async def cb_notify_del(call: CallbackQuery):
    save_id = int(call.data.split("_")[1])
    uid     = call.from_user.id
    await db.delete_saved_message(save_id)
    await call.answer("✕ Удалено", show_alert=False)
    try:
        await call.message.delete()
    except Exception:
        pass
    existing_id = home_msg.get(uid)
    if existing_id:
        try:
            await bot.edit_message_text(
                home_text(), chat_id=uid, message_id=existing_id,
                reply_markup=kb_main(uid), parse_mode="HTML"
            )
            return
        except Exception:
            pass
    sent = await bot.send_message(uid, home_text(), reply_markup=kb_main(uid))
    home_msg[uid] = sent.message_id

@dp.callback_query(F.data == "show_saved")
async def cb_show_saved(call: CallbackQuery):
    uid   = call.from_user.id
    items = await db.get_saved_messages(uid)
    await call.answer()
    if not items:
        await call.message.edit_text(
            f"◈ <b>Сохранённые сообщения</b>\n{LINE}\n\n"
            "Пусто.\n\n"
            "Когда придёт уведомление об удалённом\n"
            "или изменённом сообщении — нажми\n"
            "<b>«◆ Сохранить ➩»</b> и оно появится здесь.\n\n"
            "◇ Хранятся <b>7 дней</b>, затем удаляются автоматически.",
            reply_markup=kb_back("menu"),
        )
        return
    lines = []
    for item in items[:20]:
        icon = "✕" if item["event_type"] == "deleted" else "✦"
        preview = (item["text"][:35] + "…") if len(item["text"] or "") > 35 else (item["text"] or item["media_type"] or "—")
        from datetime import datetime as _dt
        try:
            days_left = (_dt.fromisoformat(item["expires_at"]) - _dt.now()).days + 1
        except Exception:
            days_left = 7
        lines.append(
            f"{icon} <b>{html_escape(item['from_name'] or '?')}</b>  {item['date']}\n"
            f"   {html_escape(preview)}  <i>({days_left} д.)</i>"
        )
    rows = []
    for item in items[:10]:
        icon = "✕" if item["event_type"] == "deleted" else "✦"
        name = (item["from_name"] or "?")[:12]
        rows.append([InlineKeyboardButton(
            text=f"✕ Удалить: {icon} {name}",
            callback_data=f"delsaved_{item['id']}"
        )])
    rows.append([InlineKeyboardButton(text="✕ Очистить все", callback_data="clearsaved")])
    rows.append([InlineKeyboardButton(text="← В меню",       callback_data="back_menu")])
    await call.message.edit_text(
        f"◈ <b>Сохранённые</b> ({len(items)})\n{LINE}\n\n"
        + "\n\n".join(lines)
        + f"\n\n{LINE}\n◇ Хранятся 7 дней от перехвата.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )

@dp.callback_query(F.data.startswith("delsaved_"))
async def cb_del_saved(call: CallbackQuery):
    save_id = int(call.data.split("_")[1])
    await db.delete_saved_message(save_id)
    await call.answer("✕ Удалено")
    await cb_show_saved(call)

@dp.callback_query(F.data == "clearsaved")
async def cb_clear_saved(call: CallbackQuery):
    uid   = call.from_user.id
    items = await db.get_saved_messages(uid)
    for item in items:
        await db.delete_saved_message(item["id"])
    await call.answer("✕ Все удалены", show_alert=True)
    await call.message.edit_text(
        home_text(),
        reply_markup=kb_main(uid),
    )

@dp.callback_query(F.data == "howto")
async def cb_howto(call: CallbackQuery):
    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◆ Личный профиль (Business)", callback_data="howto_profile")],
        [InlineKeyboardButton(text="▢ Группа / Канал",            callback_data="howto_group")],
        [InlineKeyboardButton(text="← В меню",                     callback_data="back_menu")],
    ])
    await call.message.edit_text(
        f"⚙ <b>Подключение</b>\n{LINE}\n"
        "Выбери тип подключения:",
        reply_markup=kb,
    )

@dp.callback_query(F.data == "howto_profile")
async def cb_howto_profile(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"◆ <b>Подключение к профилю</b> 👁️\n"
        f"{LINE}\n\n"
        "Всего <b>3 шага</b> — и бот следит за тишиной:\n\n"
        "1️⃣ Нажми кнопку <b>«Скопировать»</b> ниже —\n"
        "   бот покажет юзернейм для копирования\n\n"
        "2️⃣ Нажми кнопку <b>«Подключить»</b> —\n"
        "   откроются настройки Telegram\n\n"
        "3️⃣ Внизу найди <b>Автоматизация чатов</b> ✦\n"
        "   и вставь скопированный юзернейм\n\n"
        f"{LINE}\n"
        "✔ Подключение доступно <b>всем</b>\n"
        "✔ После подключения удалённые и изменённые\n"
        "   сообщения будут приходить тебе мгновенно\n\n"
        "◇ <i>Свои сообщения бот не трогает — только чужие.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Скопировать", callback_data="copy_bot_username")],
            [InlineKeyboardButton(text="🔗 Подключить", url="tg://settings/edit")],
            [InlineKeyboardButton(text="← Назад", callback_data="howto")],
        ]),
    )

@dp.callback_query(F.data == "copy_bot_username")
async def cb_copy_bot_username(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"📋 <b>Юзернейм бота для копирования:</b>\n"
        f"{LINE}\n\n"
        f"<code>@{BOT_USERNAME}</code>\n\n"
        f"{LINE}\n"
        "👆 <i>Нажми и удерживай юзернейм выше —\n"
        "появится меню «Копировать»</i>\n\n"
        "Затем: <b>«Подключить»</b> → внизу\n"
        "<b>Автоматизация чатов</b> → вставить",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Подключить", url="tg://settings/edit")],
            [InlineKeyboardButton(text="← Назад", callback_data="howto_profile")],
        ]),
    )

@dp.callback_query(F.data == "howto_group")
async def cb_howto_group(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"▢ <b>Подключение к группе / каналу</b>\n{LINE}\n"
        "Бот работает бесплатно — Telegram Business не нужен!\n\n"
        f"1️⃣ Добавь <code>@{BOT_USERNAME}</code> в группу или канал\n"
        "2️⃣ Дай боту права <b>Администратора</b>\n"
        "   (нужно: читать сообщения)\n"
        "3️⃣ Для групп: отключи Privacy Mode через\n"
        "   @BotFather → /setprivacy → Disabled\n"
        f"{LINE}\n"
        "✔ Готово! Теперь в группе/канале можно\n"
        "писать <code>.ai вопрос</code> — бот ответит прямо там.\n\n"
        "◇ <i>Пример: </i><code>.ai объясни квантовую физику</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить бота в группу", url=f"https://t.me/{BOT_USERNAME}?startgroup=")],
            [InlineKeyboardButton(text="← Назад", callback_data="howto")],
        ]),
    )

@dp.callback_query(F.data == "referrals")
async def cb_referrals(call: CallbackQuery):
    uid  = call.from_user.id
    refs = await db.count_referrals(uid)
    await call.answer()
    await call.message.edit_text(
        f"⟡ <b>Приглашения</b>\n{LINE}\n"
        "Пригласи близких — помоги проекту расти.\n\n"
        f"◇ Твоя ссылка:\n<code>{ref_link(uid)}</code>\n\n"
        f"◆ Приглашено: <b>{refs}</b>\n\n"
        "Доступ остаётся бесплатным для всех —\n"
        "приглашения помогают развивать проект.",
        reply_markup=kb_back("menu"),
    )

@dp.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery):
    uid    = call.from_user.id
    cached = await db.count_messages(uid)
    refs   = await db.count_referrals(uid)
    await call.answer()
    await call.message.edit_text(
        f"◆ <b>Твой профиль</b>\n{LINE}\n"
        f"◇ В архиве:     <b>{cached}</b>\n"
        f"◇ Приглашено:   <b>{refs}</b>\n"
        f"◇ Перехват:     <b>безлимит</b>\n"
        f"◇ Поиск:        <b>включён</b>\n"
        f"◇ ИИ:           <b>безлимит</b>\n"
        f"{LINE}\n"
        f"Quiet Mod — бесплатно и без лимитов. Навсегда.",
        reply_markup=kb_back("menu"),
    )

@dp.callback_query(F.data == "clear_cache")
async def cb_clear(call: CallbackQuery):
    count = await db.clear_messages(call.from_user.id)
    await call.answer(f"✕ Удалено {count} записей", show_alert=True)

@dp.callback_query(F.data == "show_all")
async def cb_show_all(call: CallbackQuery):
    uid      = call.from_user.id
    messages = await db.get_recent_messages(uid, 20)
    if not messages:
        await call.answer("▣ Архив пуст", show_alert=True)
        return
    lines = []
    for m in messages:
        preview = (m["text"][:40] + "…") if len(m["text"] or "") > 40 else (m["text"] or m["media_type"])
        lines.append(f"◆ <b>{m['from_name']}</b>  {m['date']}\n   {preview}")
    await call.answer()
    archive_rows = []
    archive_rows.append([InlineKeyboardButton(text="◐ Поиск по архиву", callback_data="search")])
    archive_rows.append([InlineKeyboardButton(text="✕ Очистить архив", callback_data="clear_cache")])
    archive_rows.append([InlineKeyboardButton(text="← В меню", callback_data="back_menu")])
    await call.message.edit_text(
        f"▣ <b>Последние {len(messages)} записей</b>\n{LINE}\n" + "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=archive_rows),
    )

@dp.callback_query(F.data.startswith("ack_"))
async def cb_ack(call: CallbackQuery):
    uid = call.from_user.id
    await call.answer("✔ Принято")
    await call.message.edit_text(
        home_text(),
        reply_markup=kb_main(uid),
    )

@dp.callback_query(F.data.startswith("del_"))
async def cb_del(call: CallbackQuery):
    msg_id  = int(call.data.split("_")[1])
    uid     = call.from_user.id
    await db.delete_message(uid, msg_id)
    await call.answer("✕ Удалено из архива")
    await call.message.edit_text(
        home_text(),
        reply_markup=kb_main(uid),
    )

@dp.callback_query(F.data == "donate")
async def cb_donate(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"⟡ <b>Поддержать развитие</b>\n{LINE}\n\n"
        "Quiet Mod бесплатен для всех —\n"
        "без лимитов, подписок и VIP. Навсегда.\n\n"
        "Мы никого ни о чём не просим.\n"
        "Но если у тебя есть немного лишнего —\n"
        "небольшой вклад очень поможет: серверы,\n"
        "ИИ и новые возможности.\n\n"
        "◇ <b>На что идут звёзды:</b>\n"
        "  • Стабильная работа 24/7\n"
        "  • Оплата ИИ для всех без лимитов\n"
        "  • Новые фичи и улучшения\n\n"
        "Спасибо, что ты с нами 👁️",
        reply_markup=kb_donate(),
    )

@dp.callback_query(F.data.startswith("pay_"))
async def cb_pay(call: CallbackQuery):
    parts = call.data.split("_")
    stars = int(parts[2])
    title       = f"⟡ Вклад {stars}⭐"
    description = f"Поддержка развития Quiet Mod — {stars} звёзд"
    await call.answer()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=title,
        description=description,
        payload=f"donate_{stars}",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars)],
    )

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def on_payment(msg: Message):
    uid     = msg.from_user.id
    stars   = msg.successful_payment.total_amount
    payload = msg.successful_payment.invoice_payload
    await db.save_payment(uid, stars, payload)
    text = (
        f"⟡ <b>Спасибо за поддержку!</b>\n{LINE}\n\n"
        f"Ты внёс вклад в развитие Quiet Mod — <b>{stars}⭐</b>\n\n"
        "Эти средства пойдут на серверы, ИИ и новые возможности.\n\n"
        "Бот остаётся бесплатным и безлимитным для всех — навсегда.\n"
        "Именно такие люди, как ты, делают это возможным 👁️"
    )
    await msg.answer(text, reply_markup=kb_back("menu"))
    try:
        await bot.send_message(
            ADMIN_ID,
            f"⟡ <b>Донат</b> · {payload}\n"
            f"◇ {msg.from_user.full_name} (ID: {uid})\n"
            f"⭐ {stars} звёзд",
        )
    except Exception:
        pass
