"""Старт (/start), админ-команда (/admin) и подключение Business."""
import asyncio
from typing import Optional

from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BusinessConnection, Message
from html import escape as html_escape

import database as db
from business_api import _BC_OWNER_CACHE, _BC_OWNER_TTL_SECONDS
from core import ADMIN_ID, bot, dp, log
from functions import LINE, _show_home, kb_admin, kb_main, ref_link


@dp.business_connection()
async def on_business_connection(conn: BusinessConnection):
    """Предзагружаем владельца подключения — мьют/перехват работают мгновенно с первого сообщения."""
    try:
        _BC_OWNER_CACHE[conn.id] = (
            conn.user.id,
            asyncio.get_running_loop().time() + _BC_OWNER_TTL_SECONDS,
        )
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
    home_text_full = (
        f"◆ <b>QUIET MOD</b> 👁️\n"
        f"<code>{LINE}</code>\n\n"
        f"<b>{html_escape(name)}</b>, добро пожаловать в тишину.\n\n"
        "Я слежу за тем, что исчезает —\n"
        "<b>удалённые и изменённые</b> сообщения\n"
        "появятся здесь раньше, чем их забудут.\n\n"
        f"<code>{LINE}</code>\n"
        f"◇ Статус       <b>Свободен · без лимитов</b>\n"
        f"◇ Перехват     <b>безлимит</b>\n"
        f"◇ Архив        <b>безлимит</b>\n"
        f"◇ Поиск        <b>включён</b>\n"
        f"◇ ИИ           <b>без лимитов</b>\n"
        f"<code>{LINE}</code>\n\n"
        f"◇ Пригласить:\n"
        f"<code>{ref_link(uid)}</code>"
    )
    await _show_home(uid, home_text_full, kb_main(uid), msg)


@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(
        f"▲ <b>Admin Suite</b>\n{LINE}",
        reply_markup=kb_admin(),
    )
