"""🛰 .sled — отслеживание изменений профиля Telegram-пользователя.

Команды (только в ЛС):
    .sled @username|id  — добавить цель (макс 3 на пользователя)
    .sled               — список целей с inline-кнопками
    .unsled @username   — убрать цель
    .infosled @username — история всех зафиксированных изменений

Фоновая задача _sled_loop раз в SLED_CHECK_INTERVAL_SECONDS опрашивает
getChat + getUserProfilePhotos для каждой цели, сравнивает с прошлым
состоянием и шлёт владельцу уведомление.

Ограничения Telegram Bot API:
  • онлайн/время в сети — недоступны боту (нет методов);
  • смена username ловится по расхождению текущего username с last_username;
  • смена аватарки ловится по total_count и file_id последней фотографии.
"""
import asyncio
from html import escape as html_escape
from datetime import datetime, timezone, timedelta

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from core import bot, dp, log
import database as db
from functions import LINE, kb_back

SLED_CHECK_INTERVAL_SECONDS = 5 * 60     # опрос каждые 5 минут
SLED_SEMAPHORE_LIMIT = 5                 # конкурентных getChat одновременно
SLED_EVENTS_TTL_DAYS = 30                # хранить события 30 дней
SLED_MAX_TARGETS = 3                     # макс целей на пользователя
_MSK = timezone(timedelta(hours=3))      # МСК для человеко-читаемых дат


# ─── helpers ───────────────────────────────────────────────────────
def _fmt_dt(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone(_MSK).strftime("%d.%m %H:%M")
    except Exception:
        return ""


async def _resolve_target(raw: str) -> dict | None:
    """Резолвит @username или id → {id, full_name, username, bio?}."""
    s = raw.lstrip("@").strip()
    if not s:
        return None
    try:
        chat = await bot.get_chat(int(s) if s.isdigit() else s)
    except Exception as e:
        log.debug(f"🛰 sled resolve '{s}': {e}")
        return None
    return {
        "id": chat.id,
        "full_name": getattr(chat, "full_name", "") or "",
        "username": (getattr(chat, "username", "") or "").lstrip("@"),
        "bio": getattr(chat, "bio", "") or "",
    }


def _target_label(t: dict) -> str:
    name = (t.get("target_name") or t.get("target_name") or "").strip()
    uname = t.get("target_username") or ""
    if name and uname:
        return f"{name} (@{uname})"
    return name or (f"@{uname}" if uname else str(t["target_id"]))


def _kb_targets(targets: list) -> InlineKeyboardMarkup:
    rows = []
    for t in targets:
        rows.append([InlineKeyboardButton(
            text=f"🛰 {_target_label(t)}",
            callback_data=f"sled_info:{t['target_id']}",
        )])
    rows.append([InlineKeyboardButton(text="◇ Удалить цель…",
                                      callback_data="sled_pick_del")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─── .sled ─────────────────────────────────────────────────────────
@dp.message(StateFilter("*"),
            F.text.regexp(r"(?i)^\.sled(@\S+|\s+@?\S+)?\s*$"),
            F.chat.type == "private")
async def on_sled(msg: Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    raw = (msg.text or "").strip()
    body = raw[5:].strip() if len(raw) > 5 else ""

    if not body:
        targets = await db.get_user_sled_targets(uid)
        if not targets:
            await msg.answer(
                f"🛰 <b>СЛЕД</b>\n{LINE}\n\n"
                "Отслеживай изменения профиля —\n"
                "имя, юзернейм, bio, аватарка.\n\n"
                "◇ <code>.sled @username</code> — добавить цель\n"
                "◇ <code>.unsled @username</code> — убрать\n"
                "◇ <code>.infosled @username</code> — история\n\n"
                f"◇ Максимум целей: <b>{SLED_MAX_TARGETS}</b>\n"
                f"◇ Опрос каждые: <b>{SLED_CHECK_INTERVAL_SECONDS // 60} мин</b>\n\n"
                "<i>Онлайн и «был в сети» не отслеживаются —\n"
                "это ограничение Telegram Bot API.</i>",
                reply_markup=kb_back("menu"),
            )
            return
        lines = []
        for t in targets:
            lines.append(f"🛰 <b>{html_escape(_target_label(t))}</b>")
        await msg.answer(
            f"🛰 <b>СЛЕД · твои цели ({len(targets)}/{SLED_MAX_TARGETS})</b>\n"
            f"{LINE}\n\n" + "\n".join(lines) +
            f"\n\n{LINE}\n"
            f"◇ Нажми на цель — посмотреть историю.\n"
            f"◇ «Удалить цель…» — снять слежку.",
            reply_markup=_kb_targets(targets),
        )
        return

    target_str = body.lstrip("@").strip()
    count = await db.count_sled_targets(uid)
    if count >= SLED_MAX_TARGETS:
        await msg.answer(
            f"🛑 <b>Лимит</b>: максимум {SLED_MAX_TARGETS} цели.\n"
            f"◇ Сними одну: <code>.unsled @username</code>."
        )
        return
    target = await _resolve_target(target_str)
    if not target:
        await msg.answer(
            f"◇ Не нашёл <code>@{html_escape(target_str)}</code>.\n"
            f"Проверь юзернейм или открой профиль (боты/приватные)."
        )
        return
    added = await db.add_sled_target(uid, target)
    if not added:
        await msg.answer("◇ Уже следишь за этим пользователем.")
        return
    log.info(f"🛰 sled add uid={uid} target={target['id']} (@{target['username']})")
    await msg.answer(
        f"🛰 <b>СЛЕД ВКЛЮЧЁН</b>\n{LINE}\n\n"
        f"◇ Цель: <b>{html_escape(target['full_name'])}</b> "
        f"(<code>@{html_escape(target['username'])}</code>)\n"
        f"◇ ID: <code>{target['id']}</code>\n\n"
        f"{LINE}\n"
        f"◇ Опрос каждые: <b>{SLED_CHECK_INTERVAL_SECONDS // 60} мин</b>\n"
        f"◇ Слежу за: имя, юзернейм, bio, аватарка\n\n"
        f"◇ История: <code>.infosled @{html_escape(target['username'])}</code>",
        reply_markup=kb_back("menu"),
    )


# ─── .unsled ───────────────────────────────────────────────────────
@dp.message(StateFilter("*"),
            F.text.regexp(r"(?i)^\.unsled(@\S+|\s+@?\S+)?\s*$"),
            F.chat.type == "private")
async def on_unsled(msg: Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    raw = (msg.text or "").strip()
    body = raw[8:].strip() if len(raw) > 8 else ""
    if not body:
        await msg.answer("◇ Формат: <code>.unsled @username</code> или <code>.unsled id</code>")
        return
    target_str = body.lstrip("@").strip()
    if not target_str:
        return
    target_id: int | None = None
    if target_str.isdigit():
        target_id = int(target_str)
    else:
        for t in await db.get_user_sled_targets(uid):
            if (t.get("target_username") or "").lower() == target_str.lower():
                target_id = t["target_id"]
                break
    if target_id is None:
        await msg.answer(
            f"◇ Не нашёл цель «<code>{html_escape(target_str)}</code>» в твоём списке."
        )
        return
    if await db.remove_sled_target(uid, target_id):
        log.info(f"🛰 sled remove uid={uid} target={target_id}")
        await msg.answer(
            f"🛰 Снято слежки с <code>{target_id}</code>.",
            reply_markup=kb_back("menu"),
        )
    else:
        await msg.answer("◇ Этой цели нет в твоём списке.")


# ─── .infosled ─────────────────────────────────────────────────────
def _format_event(ev: dict) -> str:
    et = ev["event_type"]
    old = (ev.get("old_value") or "").strip()
    new = (ev.get("new_value") or "").strip()
    when = _fmt_dt(ev.get("created_at") or "")
    if et == "photo":
        line = f"🖼 <b>Сменил аватарку</b>"
        if old or new:
            line += f"  <i>(было фото: {html_escape(old or '?')} · стало: {html_escape(new or '?')})</i>"
    elif et == "username":
        line = (f"🔗 <b>Юзернейм:</b> <s>@{html_escape(old or '?')}</s> → "
                f"<b>@{html_escape(new or '?')}</b>")
    elif et == "bio":
        ob = (old[:80] + "…") if len(old) > 80 else old
        nb = (new[:80] + "…") if len(new) > 80 else new
        line = "📝 <b>Сменил bio</b>\n"
        if ob:
            line += f"   <s>{html_escape(ob)}</s>\n"
        if nb:
            line += f"   <b>{html_escape(nb)}</b>"
    else:  # name
        line = (f"✏️ <b>Имя:</b> <s>{html_escape(old or '?')}</s> → "
                f"<b>{html_escape(new or '?')}</b>")
    return f"<b>{when}</b>  {line}"


@dp.message(StateFilter("*"),
            F.text.regexp(r"(?i)^\.infosled(@\S+|\s+@?\S+)?\s*$"),
            F.chat.type == "private")
async def on_infosled(msg: Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    raw = (msg.text or "").strip()
    body = raw[9:].strip() if len(raw) > 9 else ""

    targets = await db.get_user_sled_targets(uid)
    if not targets:
        await msg.answer(
            "◇ У тебя нет целей в списке. <code>.sled @username</code>",
            reply_markup=kb_back("menu"),
        )
        return

    if not body:
        kb_rows = []
        for t in targets:
            kb_rows.append([InlineKeyboardButton(
                text=f"🛰 {_target_label(t)}",
                callback_data=f"sled_info:{t['target_id']}",
            )])
        kb_rows.append([InlineKeyboardButton(text="← Назад", callback_data="back_menu")])
        await msg.answer(
            "🛰 <b>Выбери цель для просмотра истории:</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        )
        return

    target_str = body.lstrip("@").strip()
    target_id: int | None = None
    if target_str.isdigit():
        target_id = int(target_str)
    else:
        for t in targets:
            if (t.get("target_username") or "").lower() == target_str.lower():
                target_id = t["target_id"]
                break
    if target_id is None:
        await msg.answer(
            f"◇ Не нашёл цель «<code>{html_escape(target_str)}</code>» в твоём списке.",
            reply_markup=kb_back("menu"),
        )
        return
    await _send_infosled(msg.chat.id, uid, target_id, targets)


async def _send_infosled(chat_id: int, uid: int, target_id: int, targets: list | None = None):
    targets = targets if targets is not None else await db.get_user_sled_targets(uid)
    target = next((t for t in targets if t["target_id"] == target_id), None)
    if not target:
        await bot.send_message(
            chat_id, "◇ Цель не найдена.", reply_markup=kb_back("menu"),
        )
        return
    events = await db.get_sled_events(uid, target_id, limit=30)
    head = (
        f"🛰 <b>{html_escape(_target_label(target))}</b>\n"
        f"◇ ID: <code>{target_id}</code>\n"
        f"◇ Добавлен: {_fmt_dt(target.get('added_at') or '')}\n"
        f"◇ Последняя проверка: {_fmt_dt(target.get('last_check') or '') or '—'}\n"
        f"{LINE}\n"
    )
    if not events:
        await bot.send_message(
            chat_id,
            head + "\n<i>Пока ничего не зафиксировано.\n"
            f"Опрос каждые {SLED_CHECK_INTERVAL_SECONDS // 60} мин — нужно время.</i>",
            reply_markup=kb_back("menu"),
        )
        return
    body = "\n\n".join(_format_event(e) for e in events)
    text = head + "\n" + body
    if len(text) > 4000:
        text = text[:3950] + "\n…"
    await bot.send_message(chat_id, text, reply_markup=kb_back("menu"))


# ─── callback'и (просмотр/удаление) ────────────────────────────────
@dp.callback_query(F.data.startswith("sled_info:"))
async def cb_sled_info(call: CallbackQuery):
    target_id = int(call.data.split(":", 1)[1])
    await call.answer()
    await _send_infosled(call.message.chat.id, call.from_user.id, target_id)


@dp.callback_query(F.data == "sled_pick_del")
async def cb_sled_pick_del(call: CallbackQuery):
    uid = call.from_user.id
    targets = await db.get_user_sled_targets(uid)
    if not targets:
        await call.answer("Нет целей", show_alert=True)
        return
    rows = [[InlineKeyboardButton(
        text=f"✕ {_target_label(t)}",
        callback_data=f"sled_del:{t['target_id']}",
    )] for t in targets]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="back_sled")])
    await call.answer()
    await call.message.edit_text(
        "🛰 <b>Какую цель снять со слежки?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("sled_del:"))
async def cb_sled_del(call: CallbackQuery):
    uid = call.from_user.id
    target_id = int(call.data.split(":", 1)[1])
    ok = await db.remove_sled_target(uid, target_id)
    await call.answer("Снято" if ok else "Не найдено", show_alert=not ok)
    targets = await db.get_user_sled_targets(uid)
    if targets:
        await call.message.edit_text(
            f"🛰 <b>СЛЕД · твои цели ({len(targets)}/{SLED_MAX_TARGETS})</b>\n"
            f"{LINE}\n\n" + "\n".join(
                f"🛰 <b>{html_escape(_target_label(t))}</b>" for t in targets
            ) + f"\n\n{LINE}\n◇ Нажми на цель — посмотреть историю.",
            reply_markup=_kb_targets(targets),
        )
    else:
        await call.message.edit_text(
            "🛰 Список целей пуст. <code>.sled @username</code>",
            reply_markup=kb_back("menu"),
        )


@dp.callback_query(F.data == "back_sled")
async def cb_back_sled(call: CallbackQuery):
    uid = call.from_user.id
    targets = await db.get_user_sled_targets(uid)
    if not targets:
        await call.message.edit_text(
            f"🛰 <b>СЛЕД</b>\n{LINE}\n\n"
            "Отслеживай изменения профиля —\n"
            "имя, юзернейм, bio, аватарка.\n\n"
            f"◇ Максимум целей: <b>{SLED_MAX_TARGETS}</b>\n"
            f"◇ Опрос каждые: <b>{SLED_CHECK_INTERVAL_SECONDS // 60} мин</b>",
            reply_markup=kb_back("menu"),
        )
    else:
        await call.message.edit_text(
            f"🛰 <b>СЛЕД · твои цели ({len(targets)}/{SLED_MAX_TARGETS})</b>\n"
            f"{LINE}\n\n" + "\n".join(
                f"🛰 <b>{html_escape(_target_label(t))}</b>" for t in targets
            ) + f"\n\n{LINE}\n◇ Нажми на цель — посмотреть историю.",
            reply_markup=_kb_targets(targets),
        )
    await call.answer()


# ─── фоновый опрос ─────────────────────────────────────────────────
async def _check_one(uid: int, target: dict) -> list:
    """Один опрос одной цели. Возвращает список событий (diff'ов)."""
    target_id = target["target_id"]
    try:
        chat = await bot.get_chat(target_id)
    except Exception as e:
        log.debug(f"🛰 sled get_chat uid={uid} target={target_id}: {e}")
        return []
    new_state = {
        "name": (getattr(chat, "full_name", "") or "").strip(),
        "username": (getattr(chat, "username", "") or "").lstrip("@").strip(),
        "bio": (getattr(chat, "bio", "") or "").strip(),
    }
    try:
        photos = await bot.get_user_profile_photos(target_id, limit=1)
        new_state["photo_count"] = photos.total_count or 0
        if photos.photos:
            new_state["last_photo_id"] = photos.photos[0][-1].file_id
        else:
            new_state["last_photo_id"] = ""
    except Exception as e:
        log.debug(f"🛰 sled photos uid={uid} target={target_id}: {e}")
        new_state["photo_count"] = target.get("last_photo_count", 0) or 0
        new_state["last_photo_id"] = target.get("last_photo_id", "") or ""

    events: list = []
    last_name = (target.get("last_name") or "").strip()
    if new_state["name"] and new_state["name"] != last_name:
        events.append({
            "event_type": "name",
            "old_value": last_name,
            "new_value": new_state["name"],
            "file_id": None,
        })
    last_username = (target.get("last_username") or "").lstrip("@").strip()
    # если раньше username был, а теперь пусто — это тоже смена
    if new_state["username"] != last_username:
        events.append({
            "event_type": "username",
            "old_value": last_username,
            "new_value": new_state["username"],
            "file_id": None,
        })
    last_bio = target.get("last_bio") or ""
    if new_state["bio"] != last_bio:
        events.append({
            "event_type": "bio",
            "old_value": last_bio,
            "new_value": new_state["bio"],
            "file_id": None,
        })
    last_count = int(target.get("last_photo_count") or 0)
    new_count = int(new_state.get("photo_count") or 0)
    last_photo_id = (target.get("last_photo_id") or "").strip()
    new_photo_id = (new_state.get("last_photo_id") or "").strip()
    if new_count != last_count or (new_photo_id and new_photo_id != last_photo_id):
        events.append({
            "event_type": "photo",
            "old_value": str(last_count),
            "new_value": str(new_count),
            "file_id": new_photo_id or None,
        })

    await db.update_sled_target_state(uid, target_id, new_state)
    return events


async def _notify_owner(uid: int, target: dict, events: list):
    if not events:
        return
    uname = f"@{target.get('target_username')}" if target.get('target_username') \
        else f"id{target['target_id']}"
    head = f"🛰 <b>{html_escape(target.get('target_name') or uname)}</b>  {uname}\n{LINE}\n"
    lines = []
    photo_events = [e for e in events if e["event_type"] == "photo"]
    text_events = [e for e in events if e["event_type"] != "photo"]
    for ev in text_events:
        lines.append(_format_event(ev))
    body = head + ("\n\n".join(lines) if lines else "")
    try:
        await bot.send_message(
            uid, body + (f"\n\n{LINE}\n◇ <code>.infosled {uname}</code>" if body else ""),
        )
    except Exception as e:
        log.warning(f"🛰 sled notify uid={uid}: {e}")
    for ev in photo_events:
        if not ev.get("file_id"):
            continue
        try:
            await bot.send_photo(
                uid, ev["file_id"],
                caption=f"🛰 Новая аватарка {uname}",
            )
        except Exception as e:
            log.warning(f"🛰 sled photo send uid={uid}: {e}")


async def _sled_loop():
    """Фоновый цикл: опрос всех целей раз в SLED_CHECK_INTERVAL_SECONDS."""
    await asyncio.sleep(15)  # даём боту прогреться
    while True:
        try:
            all_targets = await db.get_all_sled_targets_with_state()
            if all_targets:
                sem = asyncio.Semaphore(SLED_SEMAPHORE_LIMIT)

                async def _run(uid: int, target: dict):
                    async with sem:
                        try:
                            evs = await _check_one(uid, target)
                            if evs:
                                await db.save_sled_events(uid, target["target_id"], evs)
                                await _notify_owner(uid, target, evs)
                        except Exception as e:
                            log.warning(
                                f"🛰 sled check uid={uid} "
                                f"target={target['target_id']}: {e}"
                            )

                # лёгкая пауза между запросами, чтобы не упереться в flood
                tasks = []
                for uid, t in all_targets:
                    tasks.append(asyncio.create_task(_run(uid, t)))
                    await asyncio.sleep(0.05)
                await asyncio.gather(*tasks, return_exceptions=True)

            # очистка старых событий 1 раз в ~6 часов
            loop_tick = datetime.now()
            if not hasattr(_sled_loop, "_last_purge"):
                _sled_loop._last_purge = loop_tick  # type: ignore[attr-defined]
            elif (loop_tick - _sled_loop._last_purge).total_seconds() > 6 * 3600:  # type: ignore[attr-defined]
                _sled_loop._last_purge = loop_tick  # type: ignore[attr-defined]
                try:
                    removed = await db.purge_old_sled_events(SLED_EVENTS_TTL_DAYS)
                    if removed:
                        log.info(f"🛰 sled purge removed={removed}")
                except Exception as e:
                    log.warning(f"🛰 sled purge: {e}")

            await asyncio.sleep(SLED_CHECK_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"🛰 sled_loop: {e}")
            await asyncio.sleep(30)
