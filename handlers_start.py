"""Старт (/start), админ-команда (/admin) и подключение Business."""
import asyncio
from typing import Optional

from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BusinessConnection, Message

import database as db
from business_api import _BC_OWNER_CACHE, _BC_OWNER_TTL_SECONDS
from core import ADMIN_ID, bot, dp, log
from functions import LINE, _show_home, home_text_for, kb_admin, kb_main
from handlers_gate import SUB_GATE_TEXT, check_subscription, sub_kb


@dp.business_connection()
async def on_business_connection(conn: BusinessConnection):
    """Предзагружаем владельца подключения — мьют/перехват работают мгновенно с первого сообщения.

    Заодно фиксируем, кто именно подключил бота к бизнес-аккаунту: Telegram сам
    присылает юзера в апдейте business_connection — так в админке видно реальную
    базу пользователей, а не только тех, кто писал /start.
    """
    try:
        _BC_OWNER_CACHE[conn.id] = (
            conn.user.id,
            asyncio.get_running_loop().time() + _BC_OWNER_TTL_SECONDS,
        )
        try:
            await db.upsert_business_owner(
                conn.user.id,
                conn.user.username or "",
                conn.user.full_name or "",
                conn.id,
            )
            await db.upsert_user(
                conn.user.id,
                conn.user.username or "",
                conn.user.full_name or "",
            )
        except Exception as e:
            log.warning(f"business owner save: {e}")
    except Exception:
        pass


@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    uid   = msg.from_user.id
    name  = msg.from_user.full_name or "—"
    uname = msg.from_user.username or ""
    referrer_id: Optional[int] = None
    parts = msg.text.split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            rid = int(parts[1][4:])
            if rid != uid:
                referrer_id = rid
        except ValueError:
            pass
    existing = await db.get_user(uid)
    await db.upsert_user(uid, uname, name, referrer_id if not existing else None)
    if not existing and referrer_id:
        try:
            await bot.send_message(
                referrer_id,
                f"◆ <b>Новый реферал</b>\n{LINE}\n"
                f"<b>{name}</b> присоединился по твоей ссылке.",
            )
        except Exception:
            pass
    if not await check_subscription(uid):
        await msg.answer(SUB_GATE_TEXT, reply_markup=sub_kb())
        return
    try:
        await db.mark_user_subscribed(uid, True)
    except Exception:
        pass
    await _show_home(uid, home_text_for(uid, name), kb_main(uid), msg)


@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(
        f"▲ <b>Admin Suite</b>\n{LINE}",
        reply_markup=kb_admin(),
    )
