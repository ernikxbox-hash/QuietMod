"""Админка (/admin), .cmd список функций, рассылки и перехват групповых сообщений."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
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
from core import (
    ADMIN_ID,
    CHANNEL_URL,
    CHANNEL_USERNAME,
    GROQ_API_KEYS,
    GROQ_MODEL,
    S,
    bot,
    dp,
    get_http,
    log,
)
from handlers_gate import _resolve_channel, check_subscription_status, set_channel_id
from functions import (
    LINE,
    CMD_FEATURES,
    CUSTOM_EMOJI_CMD,
    MSK,
    _edit_ai_html,
    kb_admin,
    kb_back,
    kb_cmd,
    resolve_username_to_chat,
)
from handlers_games import _knb_cache_member
from handlers_intercept import (
    _cache_transcript,
    _transcribe_voice,
    _tsc_kb,
    _tsc_teaser,
)
from handlers_level import award_chat_xp


def _is_admin(call: CallbackQuery) -> bool:
    return call.from_user.id == ADMIN_ID


@dp.callback_query(F.data == "adm")
async def cb_adm(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call):
        await call.answer("✕", show_alert=True)
        return
    await state.clear()
    await call.answer()
    await call.message.edit_text(
        f"▲ <b>Admin Suite</b> · Quiet Mod 👁️\n"
        f"<code>{LINE}</code>\n\n"
        "◇ Выбери раздел:",
        reply_markup=kb_admin(),
    )


# ── 📊 Дашборд: главные цифры + тренд запусков за 7 дней ──────────────
async def _render_dashboard() -> str:
    since_24h = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)).isoformat()
    today = _stat_since(0)
    week  = _stat_since(6)
    results = await asyncio.gather(
        db.count_users(),
        db.count_users_since(since_24h),
        db.total_messages_all(),
        db.count_messages_since(since_24h),
        db.count_stats("launch", today),
        db.count_stats("launch", week),
        db.count_stats("launch"),
        db.count_stats("caught_deleted"),
        db.count_stats("caught_deleted", today),
        db.count_stats("caught_edited"),
        db.count_stats("caught_edited", today),
        db.count_stats("whisper_ok"),
        db.total_stars(),
        db.count_ideas(),
        db.count_bot_chats(),
        db.count_business_owners(),
        db.count_subscribed_users(),
        *[db.count_stats("launch", _stat_since(d)) for d in range(7)],
    )
    (users, users24, msgs, msgs24,
     l_today, l_week, l_all,
     del_all, del_today, ed_all, ed_today,
     whisper, stars, ideas, chats, biz, real) = results[:17]
    day_counts = results[17:]
    lines = [
        "◆ <b>ДАШБОРД</b> · Quiet Mod 👁️",
        f"<code>{LINE}</code>",
        f"◇ Пользователи   <b>{users}</b>  (+{users24} за 24ч)",
        f"◇ Реальные       <b>{real}</b>  подписаны на канал",
        f"◇ Бизнес         <b>{biz}</b>  подключений",
        f"◇ Сообщений      <b>{msgs}</b>  (+{msgs24} за 24ч)",
        f"◇ Запуски        сегодня <b>{l_today}</b> · 7д <b>{l_week}</b> · всего <b>{l_all}</b>",
        f"<code>{LINE}</code>",
        f"✕ Удалённых      <b>{del_all}</b>  (сегодня {del_today})",
        f"✦ Изменённых     <b>{ed_all}</b>  (сегодня {ed_today})",
        f"◇ Расшифровок    <b>{whisper}</b>",
        f"⟡ Звёзд          <b>{stars}</b>  ·  ✦ Идей <b>{ideas}</b>",
        f"▤ Бот в чатах    <b>{chats}</b>",
        f"<code>{LINE}</code>",
        "◆ <b>Запуски · 7 дней</b>",
    ]
    for d in range(6, -1, -1):
        cnt = day_counts[d]
        label = (datetime.now(MSK) - timedelta(days=d)).strftime("%d.%m")
        lines.append(f"{label}  {'█' * min(cnt, 12) or '▏'} {cnt}")
    lines += [
        f"<code>{LINE}</code>",
        f"◐ обновлено {datetime.now(MSK).strftime('%H:%M:%S')}",
    ]
    return "\n".join(lines)


@dp.callback_query(F.data == "adm_dash")
async def cb_adm_dash(call: CallbackQuery):
    if not _is_admin(call):
        return
    await call.answer()
    await call.message.edit_text(
        await _render_dashboard(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⟳ Обновить", callback_data="adm_dash")],
            [InlineKeyboardButton(text="← В меню", callback_data="adm")],
        ]),
    )


# ── 🛡 Гейт подписки: диагностика прямо из админки ─────────────────────
@dp.callback_query(F.data == "adm_gate")
async def cb_adm_gate(call: CallbackQuery):
    if not _is_admin(call):
        return
    await call.answer()
    lines = [f"◆ <b>Гейт подписки</b>\n<code>{LINE}</code>"]
    if not CHANNEL_USERNAME.strip():
        lines.append("◇ Канал: <b>не задан</b> (CHANNEL_USERNAME) — гейт выключен")
    else:
        lines.append(f"◇ Канал: <a href=\"{CHANNEL_URL}\">@{CHANNEL_USERNAME}</a>")
        chat_id = await _resolve_channel()
        if chat_id is not None:
            lines.append(f"◇ ID канала: <code>{chat_id}</code> (запомнен)")
            try:
                me = await bot.get_chat_member(chat_id, bot.id)
                lines.append(f"◇ Бот в канале: <b>да</b> ({me.status})")
            except Exception:
                lines.append("◇ Бот в канале: <b>НЕТ</b> — добавь бота администратором")
        else:
            lines.append("◇ Бот в канале: <b>НЕТ</b> — канал не найден / бот не добавлен")
        st = await check_subscription_status(ADMIN_ID, fresh=True)
        if st is True:
            lines.append("◇ Твоя подписка: <b>подписан</b> — доступ работает")
        elif st is False:
            lines.append("◇ Твоя подписка: <b>НЕ подписан</b> — доступ закрыт!")
        else:
            lines.append("◇ Твоя подписка: не удалось проверить (сбой API)")
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⟳ Обновить", callback_data="adm_gate")],
            [InlineKeyboardButton(text="← В меню", callback_data="adm")],
        ]),
    )


# ── 🔑 Живая проверка API-ключей Groq ──────────────────────────────────
async def _probe_groq_key(key: str) -> tuple[bool, str]:
    """Мини-запрос к Groq (1 токен): проверяем, что ключ живой."""
    try:
        session = get_http()
        async with session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                return True, "работает"
            return False, f"HTTP {resp.status}: {(await resp.text())[:100]}"
    except Exception as e:
        return False, str(e)[:100]


@dp.callback_query(F.data == "adm_keys")
async def cb_adm_keys(call: CallbackQuery):
    if not _is_admin(call):
        return
    await call.answer()
    if not GROQ_API_KEYS:
        await call.message.edit_text(
            f"◆ <b>Проверка API-ключей</b>\n<code>{LINE}</code>\n\nКлючи не настроены.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="← В меню", callback_data="adm")]
            ]),
        )
        return
    await call.message.edit_text(
        f"◇ Проверяю {len(GROQ_API_KEYS)} ключ(а)…\n<code>{LINE}</code>\n◇ Живой запрос к Groq по каждому."
    )
    results = await asyncio.gather(*[_probe_groq_key(k) for k in GROQ_API_KEYS])
    lines = [f"◆ <b>Проверка API-ключей</b>\n<code>{LINE}</code>"]
    for i, (ok, err) in enumerate(results, start=1):
        if ok:
            lines.append(f"   Ключ {i}: <b>работает</b>")
        else:
            lines.append(f"   Ключ {i}: <b>ОШИБКА</b> — {html_escape(err)}")
    lines += [
        f"<code>{LINE}</code>",
        "◇ Модель: " + GROQ_MODEL,
    ]
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⟳ Проверить ещё раз", callback_data="adm_keys")],
            [InlineKeyboardButton(text="← В меню", callback_data="adm")],
        ]),
    )


# ── 📥 Лента последних перехватов ──────────────────────────────────────
@dp.callback_query(F.data == "adm_catches")
async def cb_adm_catches(call: CallbackQuery):
    if not _is_admin(call):
        return
    await call.answer()
    catches = await db.get_recent_catches(10)
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⟳ Обновить", callback_data="adm_catches")],
        [InlineKeyboardButton(text="← В меню", callback_data="adm")],
    ])
    if not catches:
        await call.message.edit_text(
            f"◆ <b>Последние перехваты</b>\n<code>{LINE}</code>\n\nПусто.",
            reply_markup=back_kb,
        )
        return
    lines = [f"◆ <b>Последние перехваты</b>\n<code>{LINE}</code>"]
    for c in catches:
        icon = "✕" if c["event_type"] == "deleted" else "✦"
        name = html_escape((c.get("from_name") or "?")[:24])
        chat = html_escape((c.get("chat") or "—")[:24])
        preview = html_escape((c.get("text") or c.get("media_type") or "")[:40])
        lines.append(f"{icon} <b>{name}</b> · {chat} · {c.get('date') or '—'}\n   {preview}")
    await call.message.edit_text("\n\n".join(lines), reply_markup=back_kb)


# ── ◆ Бизнес: кто подключил бота к бизнес-аккаунту ─────────────────────
@dp.callback_query(F.data == "adm_biz")
async def cb_adm_biz(call: CallbackQuery):
    """Список пользователей, подключивших бота к бизнесу (реальная база)."""
    if not _is_admin(call):
        return
    await call.answer()
    total = await db.count_business_owners()
    owners = await db.get_business_owners(20)
    lines = [f"◆ <b>Бизнес-подключения</b> · <b>{total}</b>\n<code>{LINE}</code>"]
    if not owners:
        lines.append("\nПусто — пока никто не подключал бота к бизнесу.\n\n"
                     "Как только кто-то подключит бота через «Подключение» —\n"
                     "он появится здесь с именем и @username.")
    else:
        for o in owners:
            uname = f"@{o['username']}" if o.get("username") else "—"
            name = html_escape((o.get("full_name") or "—")[:24])
            try:
                seen = datetime.fromisoformat(o["last_seen"]).astimezone(MSK).strftime("%d.%m · %H:%M")
            except Exception:
                seen = "—"
            lines.append(
                f"◇ <b>{html_escape(uname)}</b>  (ID {o['user_id']})\n"
                f"   {name} · активен: {seen}"
            )
        if total > len(owners):
            lines.append(f"\n◇ Показаны первые {len(owners)} из {total}")
    await call.message.edit_text(
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⟳ Обновить", callback_data="adm_biz")],
            [InlineKeyboardButton(text="← В меню", callback_data="adm")],
        ]),
    )


USERS_PAGE_SIZE = 10


def _fmt_user_line(u: dict) -> str:
    uname = f"@{u['username']}" if u.get("username") else (u.get("full_name") or "—")
    if u.get("referrer_id"):
        source = f"⟡ по приглашению (от ID {u['referrer_id']})"
    else:
        source = "◇ по юзернейму / прямой запуск"
    sub = "◆ подписан" if u.get("subscribed") else "◇ не подписан"
    return f"<b>{html_escape(uname)}</b>  (ID {u['id']})\n   {source} · {sub}"


async def _render_users_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_users()
    offset = page * USERS_PAGE_SIZE
    users = await db.get_all_users(limit=USERS_PAGE_SIZE, offset=offset)
    if not users:
        text = f"◆ <b>Пользователи</b>\n<code>{LINE}</code>\nВсего: <b>{total}</b>\n\nПусто."
    else:
        lines = [_fmt_user_line(u) for u in users]
        page_count = (total + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE
        text = (
            f"◆ <b>Пользователи</b>  ({total})\n<code>{LINE}</code>\n\n"
            + "\n\n".join(lines)
            + f"\n\n<code>{LINE}</code>\nСтраница {page + 1} / {max(page_count, 1)}"
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"adm_users_p{page-1}"))
    if offset + USERS_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Вперёд →", callback_data=f"adm_users_p{page+1}"))
    rows = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="◇ Поиск пользователя", callback_data="adm_users_search")])
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


@dp.callback_query(F.data == "adm_users_search")
async def cb_adm_users_search(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call):
        return
    await call.answer()
    await state.set_state(S.adm_search)
    await call.message.edit_text(
        f"◆ <b>Поиск пользователя</b>\n<code>{LINE}</code>\n\n"
        "Пришли числовой ID или @username —\n"
        "покажу карточку (регистрация, рефералы, архив):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="adm_users")]
        ]),
    )


@dp.message(S.adm_search)
async def on_adm_search(msg: Message, state: FSMContext):
    """Карточка пользователя по ID/@username: регистрация, рефералы, архив."""
    if not msg.from_user or msg.from_user.id != ADMIN_ID:
        await state.clear()
        return
    await state.clear()
    query = (msg.text or "").strip().lstrip("@")
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◇ Ещё поиск", callback_data="adm_users_search")],
        [InlineKeyboardButton(text="← К списку", callback_data="adm_users")],
        [InlineKeyboardButton(text="← В меню", callback_data="adm")],
    ])
    uid: Optional[int] = None
    if query.isdigit():
        uid = int(query)
    else:
        uid = await db.find_sender_id_by_username(query)
        if uid is None:
            resolved = await resolve_username_to_chat(query)
            if resolved:
                uid = resolved.get("id")
    if uid is None:
        await msg.answer(
            f"◆ <b>Поиск пользователя</b>\n<code>{LINE}</code>\n\n"
            f"◇ <code>{html_escape(query or '?')}</code> — не найден.\n"
            "◇ Попробуй числовой ID или точный @username.",
            reply_markup=back_kb,
        )
        return
    lines = [
        f"◆ <b>ПОЛЬЗОВАТЕЛЬ</b> · <code>{uid}</code>\n<code>{LINE}</code>",
        f"◇ Профиль: <a href=\"tg://user?id={uid}\">открыть в Telegram</a>",
    ]
    u = await db.get_user(uid)
    if u:
        uname = html_escape(f"@{u['username']}" if u.get("username") else "—")
        lines.append(f"◇ Username: {uname}")
        lines.append(f"◇ Имя: {html_escape(u.get('full_name') or '—')}")
        lines.append(
            "◇ Подписка: " + ("<b>подписан</b> на канал" if u.get("subscribed") else "<b>НЕ подписан</b>")
        )
        try:
            joined = datetime.fromisoformat(u["joined"]).astimezone(MSK).strftime("%d.%m.%Y · %H:%M")
            lines.append(f"◇ Присоединился: {joined}")
        except Exception:
            pass
        if u.get("referrer_id"):
            lines.append(f"◇ По приглашению: <code>{u['referrer_id']}</code>")
    else:
        lines.append("◇ В базе users не найден (известен только по архиву)")
    refs = await db.count_referrals(uid)
    msgs_cnt = await db.count_messages(uid)
    lines.append(f"◇ Пригласил(а): <b>{refs}</b>")
    lines.append(f"◇ В архиве: <b>{msgs_cnt}</b> сообщений")
    try:
        await msg.answer("\n".join(lines), reply_markup=back_kb)
    except Exception as e:
        log.warning(f"adm search answer: {e}")


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
            f"   ◇ Ключ {i + 1}: <b>{ok_a}</b> ok (сегодня {ok_t}) · ошибок <b>{fail_a}</b>"
        )
    if not key_lines:
        key_lines = ["   — ключи не настроены —"]
    text = (
        f"◆ <b>СТАТИСТИКА</b> · Quiet Mod 👁️\n"
        f"<code>{LINE}</code>\n"
        f"◇ Запуски        сегодня <b>{launches[0]}</b> · 7д <b>{launches[1]}</b> · "
        f"30д <b>{launches[2]}</b> · всего <b>{launches[3]}</b>\n"
        f"<code>{LINE}</code>\n"
        f"◆ Groq API · успешных <b>{ok_total}</b>\n"
        + "\n".join(key_lines)
        + f"\n<code>{LINE}</code>\n"
        f"✕ Удалённых      <b>{del_caught}</b>\n"
        f"✦ Изменённых     <b>{ed_caught}</b>\n"
        f"◇ Расшифровок    <b>{whisper}</b>\n"
        f"<code>{LINE}</code>\n"
        f"◇ Пользователей  <b>{users}</b>\n"
        f"◇ Записей в БД   <b>{msgs}</b>\n"
        f"⟡ Звёзд          <b>{stars}</b>  ·  ✦ Идей <b>{ideas}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⟳ Обновить", callback_data="adm_stats"),
            InlineKeyboardButton(text="◇ Сброс ошибок ключей", callback_data="adm_stats_reset_fails"),
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
    await call.answer(f"◇ Ошибки сброшены · удалено: {n1 + n2}", show_alert=False)
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
            f"✦ <b>Предложения от пользователей</b>\n<code>{LINE}</code>\n"
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
        f"✦ <b>Предложения от пользователей</b>  ({len(ideas)} шт.)\n<code>{LINE}</code>\n\n"
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
        f"✦ <b>Предложения от пользователей</b>\n<code>{LINE}</code>\n"
        "Список очищен.",
        reply_markup=kb_admin(),
    )


@dp.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call): return
    await call.answer()
    await state.set_state(S.broadcast)
    await call.message.edit_text(
        f"▤ <b>Сообщение всем пользователям</b>\n<code>{LINE}</code>\n\n"
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
        f"▤ <b>Рассылка завершена</b>\n<code>{LINE}</code>\n"
        f"◇ Доставлено: <b>{ok}</b>\n"
        f"✕ Не доставлено: <b>{fail}</b>",
        reply_markup=kb_admin(),
    )


@dp.callback_query(F.data == "suggest_idea")
async def cb_suggest_idea(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(S.suggest_idea)
    await call.message.edit_text(
        f"✦ <b>Предложить идею</b>\n<code>{LINE}</code>\n\n"
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
        f"✦ <b>Спасибо за идею!</b>\n<code>{LINE}</code>\n\n"
        "Твоё предложение отправлено разработчику.\n"
        "Лучшие идеи попадают в следующие обновления.\n\n"
        "Ты помогаешь сделать Quiet Mod лучше.",
        reply_markup=kb_back("menu"),
    )
    uname = f"@{msg.from_user.username}" if msg.from_user.username else msg.from_user.full_name
    try:
        await bot.send_message(
            ADMIN_ID,
            f"✦ <b>Новая идея!</b>\n<code>{LINE}</code>\n"
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
            # 🛡 Бота добавили именно в канал гейта — фиксируем ID сразу (и в БД)
            if is_gate_channel:
                await set_channel_id(chat.id)
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
            f"▤ <b>Рассылка по группам/каналам</b>\n<code>{LINE}</code>\n\n"
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
        f"▤ <b>Рассылка по группам/каналам</b>\n<code>{LINE}</code>\n\n"
        f"Бот админ в <b>{len(chats)}</b> чатах:\n"
        f"{chat_list}\n\n"
        f"<code>{LINE}</code>\n"
        "Отправь сообщение — оно будет скопировано\n"
        "во все чаты, где бот администратор.\n\n"
        "Поддерживаются текст, фото, видео и другие\n"
        "медиа с подписью — формат сохранится.\n\n"
        "✕ Для отмены — нажми кнопку ниже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="adm")]
        ]),
    )


def cmd_catalog_text() -> str:
    """Каталог команд: 4 категории, белый стиль (◆ ◇, без цветных эмодзи).

    Премиум-эмодзи (пак CPT_Emoji) стоят на КНОПКАХ функций — kb_cmd() и
    kb_cmd_main() (icon_custom_emoji_id). В ТЕКСТ кастомный эмодзи вставить
    нельзя: фолбэк внутри тега <tg-emoji> обязан быть настоящим эмодзи, иначе
    Telegram возвращает ENTITY_TEXT_INVALID и сообщение не отправляется вовсе.
    """
    return (
        "◆ <b>QUIET MOD</b> 👁️ — все команды\n"
        f"<code>{LINE}</code>\n\n"
        "▣ <b>МЕДИА</b> — из фото и видео\n"
        "<code>.stik</code> стикер · <code>.krom</code> кружок · <code>.gif</code> гифка\n"
        "<code>.ramka</code> рамка · <code>.wm</code> знак · <code>.voice</code> озвучка\n"
        "<code>.шрифт</code> стили текста\n\n"
        "✧ <b>ИИ И ИНФО</b>\n"
        "<code>.ai</code> ИИ-помощник · <code>.info</code> карточка · <code>.price</code> цена ника\n"
        "<code>.curs</code> курсы валют · <code>.sled</code> слежка за профилем\n\n"
        "◈ <b>ЛИЧНЫЙ ЧАТ</b> — защита\n"
        "<code>.mute</code> · <code>.nomute</code> · <code>.afk</code> · <code>.code</code>\n"
        "<code>.wbl</code> · <code>.black</code> · <code>.spam</code>\n\n"
        "◇ <b>ТЕКСТ И ИГРЫ</b>\n"
        "<code>.bold</code> · <code>.italic</code> · <code>.mono</code> · <code>.line</code>\n"
        "<code>.crossed</code> · <code>.hidden</code> · <code>.quote</code> · <code>.knb</code>\n"
        "<code>.level</code> · <code>.who</code>\n\n"
        f"<code>{LINE}</code>\n"
        "◇ <b>Подробнее о каждой команде</b> — кнопка ниже 👇"
    )


def kb_cmd_main() -> InlineKeyboardMarkup:
    """Кнопки под каталогом: подробный разбор по кнопке, закрыть — тут же.

    Премиум-эмодзи (пак CPT_Emoji) — иконка на кнопке «Подробнее»:
    видят премиум-пользователи, остальным — просто текст кнопки.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="◆ Подробнее",
            callback_data="cmd_menu",
            icon_custom_emoji_id=CUSTOM_EMOJI_CMD or None,
        ),
         InlineKeyboardButton(text="✕ Закрыть", callback_data="cmd_close")],
    ])


@dp.message(F.text.regexp(r"(?i)^(?:\.cmd|\.help)$"), F.chat.type.in_({"private", "group", "supergroup", "channel"}))
async def on_cmd(msg: Message):
    await msg.answer(cmd_catalog_text(), reply_markup=kb_cmd_main())


@dp.business_message(F.text.regexp(r"(?i)^(?:\.cmd|\.help)$"))
async def on_cmd_business(msg: Message):
    await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        "◆ <b>QUIET MOD</b> 👁️ — все команды",
    )
    await _business_send_message_ex(
        msg.business_connection_id, msg.chat.id,
        cmd_catalog_text(),
        reply_markup=kb_cmd_main().model_dump(exclude_none=True),
    )


@dp.callback_query(F.data == "cmd_menu")
async def cb_cmd_menu(call: CallbackQuery):
    """Кнопка «Подробнее»: полный разбор каждой команды (описание/пример)."""
    await call.answer()
    await call.message.edit_text(
        f"◆ <b>QUIET MOD</b> 👁️ — подробнее\n<code>{LINE}</code>\n\n"
        "Выбери функцию — покажу описание,\n"
        "как использовать и пример:",
        reply_markup=kb_cmd(),
    )


@dp.callback_query(F.data.startswith("cmd_info_"))
async def cb_cmd_info(call: CallbackQuery):
    key = call.data.replace("cmd_info_", "")
    feat = CMD_FEATURES.get(key)
    if not feat:
        await call.answer("Функция не найдена", show_alert=True)
        return
    text = (
        f"{feat['title']}\n<code>{LINE}</code>\n\n"
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
    await call.message.edit_text(cmd_catalog_text(), reply_markup=kb_cmd_main())


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
        f"<code>{LINE}</code>\n\n"
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
    result_parts = [f"◇ Доставлено: <b>{ok}</b>"]
    if fail:
        result_parts.append(f"✕ Ошибок: <b>{fail}</b>")
    if removed:
        result_parts.append(f"◇ Устаревших чатов удалено: <b>{removed}</b>")
    await status.edit_text(
        f"▤ <b>Рассылка по группам/каналам завершена</b>\n<code>{LINE}</code>\n" + "\n".join(result_parts),
        reply_markup=kb_admin(),
    )


# Кэш регистрации чатов/юзеров: в группах/каналах сообщения идут потоком,
# и писать add_bot_chat + upsert_user в БД на КАЖДОЕ сообщение — лишние
# транзакции с commit на горячем пути. Пишем только когда данные изменились.
_KNOWN_CHATS: dict[int, tuple[str, str]] = {}  # chat_id -> (title, chat_type)
_KNOWN_USERS: dict[int, tuple[str, str]] = {}  # user_id -> (username, full_name)


@dp.message(F.chat.type.in_({"group", "supergroup", "channel"}))
@dp.channel_post()
async def on_group_msg(msg: Message):
    """Сохраняет чат в БД при любом сообщении в группе/канале + начисляет XP."""
    if msg.chat.type in ("group", "supergroup") and msg.from_user and not msg.from_user.is_bot:
        try:
            await award_chat_xp(msg.chat.id, msg.from_user, msg)
        except Exception as e:
            log.warning(f"level xp: {e}")
    if msg.chat.type in ("group", "supergroup", "channel"):
        title = msg.chat.title or ""
        if _KNOWN_CHATS.get(msg.chat.id) != (title, msg.chat.type):
            await db.add_bot_chat(msg.chat.id, title, msg.chat.type)
            _KNOWN_CHATS[msg.chat.id] = (title, msg.chat.type)
        if msg.from_user:
            if len(_KNOWN_USERS) > 50_000:
                _KNOWN_USERS.clear()  # предохранитель памяти для очень больших групп
            uname = msg.from_user.username or ""
            fname = msg.from_user.full_name or ""
            if _KNOWN_USERS.get(msg.from_user.id) != (uname, fname):
                await db.upsert_user(msg.from_user.id, uname, fname)
                _KNOWN_USERS[msg.from_user.id] = (uname, fname)
            _knb_cache_member(msg.chat.id, msg.from_user)
    if msg.voice or msg.video_note:
        media_label = "голосового" if msg.voice else "кружка"
        try:
            thinking = await msg.reply("◆ · · ·")
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
                    answer="◇ <b>Не удалось расшифровать</b> — попробуй ещё раз."
                )
        except Exception as e:
            log.error(f"group voice/video transcription: {e}")
            try:
                await _edit_ai_html(
                    thinking,
                    prefix="",
                    answer="◇ <b>Не удалось расшифровать</b> — попробуй ещё раз."
                )
            except Exception:
                try:
                    await thinking.delete()
                except Exception:
                    pass
