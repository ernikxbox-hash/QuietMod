"""🛡 Гейт подписки: доступ к боту — только подписчикам канала.

Как работает:
- /start и любое сообщение в ЛС с ботом: если пользователь не подписан,
  вместо функций приходит экран «Подпишись на канал» с двумя кнопками:
  🔔 Подписаться (URL) и ✅ Проверить.
- Кнопка «Проверить» делает СВЕЖУЮ проверку через getChatMember и при
  успехе открывает главное меню.
- Между проверками результат кэшируется (3 минуты, RAM + БД): выход из
  канала замечаем в течение пары минут, но API не долбим на каждое
  сообщение. Если кэш протух, а юзер вышел — доступ закрывается.
- Бизнес-автоматизация (перехват, архив, уведомления) работает, только
  если ВЛАДЕЛЕЦ подключения подписан на канал. Нет подписки — сообщения
  не сохраняются и не пересылаются (владельцу уходит напоминание).
- Кнопки (callback) тоже под гейтом — кроме самой «Проверить». Кнопки
  в группах и бизнес-кнопки подписанного владельца не трогаем.
- Ошибки Telegram API (бот не добавлен в канал, сеть) НЕ блокируют бота:
  гейт временно открывается, в логах — предупреждение.
"""
import time
from datetime import datetime, timedelta
from typing import Optional

from aiogram import BaseMiddleware, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import database as db
from business_api import _get_owner_id_cached
from core import CHANNEL_URL, CHANNEL_USERNAME, bot, dp, log
from functions import LINE, home_msg, home_text_for, kb_main

_SUB_TTL_SECONDS = 3 * 60      # кэш подписки (RAM + БД)
_SUB_ERR_TTL_SECONDS = 30      # негативный кэш ошибки API: повторно не проверяем 30 сек
_DM_PROMPT_SECONDS = 60        # анти-спам: экран «подпишись» не чаще раза в минуту
_BIZ_NOTIFY_SECONDS = 10 * 60  # анти-спам: напоминание владельцу не чаще раза в 10 минут

_SUB_CACHE: dict[int, float] = {}       # user_id -> time.monotonic() подтверждения
_SUB_ERR_CACHE: dict[int, float] = {}   # user_id -> время последней ОШИБКИ API (гейт открыт)
_DM_PROMPT_LAST: dict[int, float] = {}  # user_id -> время последнего экрана гейта
_BIZ_NOTIFY_LAST: dict[int, float] = {}  # owner_id -> время последнего напоминания


def _channel_display() -> str:
    return f"@{CHANNEL_USERNAME}" if CHANNEL_USERNAME.strip() else CHANNEL_URL


SUB_GATE_TEXT = (
    f"◆ <b>QUIET MOD</b> 👁️\n"
    f"<code>{LINE}</code>\n\n"
    "Чтобы пользоваться ботом —\n"
    "подпишись на канал:\n\n"
    f"🔔 <a href=\"{CHANNEL_URL}\">{_channel_display()}</a>\n\n"
    "Затем вернись сюда и нажми\n"
    "кнопку <b>«Проверить»</b>.\n"
    f"<code>{LINE}</code>\n"
    "◇ Подписка бесплатна\n"
    "◇ Без подписки бот недоступен: ни в ЛС, ни в автоматизации"
)


def sub_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="sub_check")],
    ])


async def _api_subscribed(uid: int) -> Optional[bool]:
    """Свежая проверка через Telegram API.

    True — подписан, False — не подписан, None — проверить не удалось
    (бот не в канале / сеть / гейт выключен). None → гейт временно открыт.
    """
    if not CHANNEL_USERNAME.strip():
        return None
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, uid)
    except Exception as e:
        log.warning(f"🛡 getChatMember uid={uid}: {e} — гейт временно открыт")
        return None
    status = member.status
    if status in ("creator", "administrator", "member"):
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def check_subscription_status(uid: int, fresh: bool = False) -> Optional[bool]:
    """Три состояния: True — подписан, False — нет, None — не удалось проверить.

    fresh=False — кэш (RAM + БД, 3 мин; ошибка API кэшируется 30 сек);
    fresh=True — всегда свежий запрос API (кнопка «Проверить»).
    """
    now = time.monotonic()
    if not fresh:
        if now - _SUB_CACHE.get(uid, 0.0) < _SUB_TTL_SECONDS:
            return True
        if now - _SUB_ERR_CACHE.get(uid, 0.0) < _SUB_ERR_TTL_SECONDS:
            return None
        verified = await db.get_channel_sub(uid)
        if verified:
            try:
                if datetime.now() - datetime.fromisoformat(verified) < timedelta(seconds=_SUB_TTL_SECONDS):
                    _SUB_CACHE[uid] = now
                    return True
            except ValueError:
                pass
    ok = await _api_subscribed(uid)
    if ok is True:
        _SUB_CACHE[uid] = now
        if len(_SUB_CACHE) > 5000:
            _SUB_CACHE.clear()
            _SUB_ERR_CACHE.clear()
        try:
            await db.set_channel_sub(uid)
        except Exception:
            pass
        return True
    if ok is False:
        _SUB_CACHE.pop(uid, None)
        _SUB_ERR_CACHE.pop(uid, None)
        try:
            await db.clear_channel_sub(uid)
        except Exception:
            pass
        return False
    # Ошибка API: гейт временно открыт, но повторную проверку не делаем 30 секунд
    _SUB_ERR_CACHE[uid] = now
    return None


async def check_subscription(uid: int, fresh: bool = False) -> bool:
    """Bool-обёртка для middleware и /start: None (ошибка API) = доступ открыт."""
    return (await check_subscription_status(uid, fresh=fresh)) is not False


@dp.callback_query(F.data == "sub_check")
async def cb_sub_check(call: CallbackQuery):
    """Кнопка «✅ Проверить подписку» — свежая проверка и вход."""
    uid = call.from_user.id
    status = await check_subscription_status(uid, fresh=True)
    if status is None:
        # Не удалось спросить у Telegram — не выдаём доступ молча, но и не запираем
        try:
            await call.answer("⚠️ Не удалось проверить подписку — попробуй ещё раз", show_alert=True)
        except Exception:
            pass
        return
    if status:
        try:
            await call.answer("✅ Подписка подтверждена — доступ открыт", show_alert=False)
        except Exception:
            pass
        name = call.from_user.full_name or "—"
        try:
            home_msg[uid] = call.message.message_id
            await call.message.edit_text(home_text_for(uid, name), reply_markup=kb_main(uid))
        except Exception as e:
            log.warning(f"🛡 sub_check edit: {e}")
    else:
        try:
            await call.answer("❌ Ты ещё не подписан(а) на канал", show_alert=True)
        except Exception:
            pass
        try:
            await call.message.edit_text(
                "❌ <b>Подписка не найдена</b>.\n\n"
                f"1️⃣ Нажми <a href=\"{CHANNEL_URL}\">Подписаться на канал</a>\n"
                "2️⃣ Вернись в бота\n"
                "3️⃣ Нажми «Проверить» ещё раз",
                reply_markup=sub_kb(),
            )
        except Exception:
            pass


async def _notify_owner_gate(owner_id: int):
    """Напоминание владельцу: автоматизация приостановлена из-за отсутствия подписки."""
    now = time.monotonic()
    if now - _BIZ_NOTIFY_LAST.get(owner_id, 0.0) < _BIZ_NOTIFY_SECONDS:
        return
    _BIZ_NOTIFY_LAST[owner_id] = now
    try:
        await bot.send_message(
            owner_id,
            "🔒 <b>Автоматизация приостановлена</b>\n"
            f"{LINE}\n"
            "Ты не подписан(а) на канал — перехват, архив\n"
            "и уведомления не работают, пока подписка\n"
            "не вернётся.\n\n"
            f"1️⃣ <a href=\"{CHANNEL_URL}\">Подписаться на канал</a>\n"
            "2️⃣ Нажми /start и «Проверить»",
            reply_markup=sub_kb(),
        )
    except Exception as e:
        log.warning(f"🛡 notify owner gate {owner_id}: {e}")


class _SubGateDM(BaseMiddleware):
    """ЛС с ботом: без подписки любое сообщение (кроме /start) блокируется."""

    async def __call__(self, handler, event: Message, data: dict):
        if event.chat.type != "private" or not event.from_user:
            return await handler(event, data)
        if (event.text or "").strip().startswith("/start"):
            return await handler(event, data)  # /start сам рисует гейт
        uid = event.from_user.id
        if await check_subscription(uid):
            return await handler(event, data)
        now = time.monotonic()
        if now - _DM_PROMPT_LAST.get(uid, 0.0) >= _DM_PROMPT_SECONDS:
            _DM_PROMPT_LAST[uid] = now
            try:
                await event.answer(SUB_GATE_TEXT, reply_markup=sub_kb())
            except Exception as e:
                log.warning(f"🛡 gate prompt: {e}")
        return None


class _SubGateBiz(BaseMiddleware):
    """Бизнес-чаты: автоматизация работает только для подписанного владельца."""

    async def __call__(self, handler, event, data: dict):
        conn_id = getattr(event, "business_connection_id", None)
        if not conn_id:
            return await handler(event, data)
        owner_id = await _get_owner_id_cached(conn_id, "gate")
        if owner_id is None:
            return await handler(event, data)
        if await check_subscription(owner_id):
            return await handler(event, data)
        await _notify_owner_gate(owner_id)
        return None


class _SubGateCB(BaseMiddleware):
    """Кнопки: обычные — по подписке нажавшего; бизнес — по владельцу; группы — мимо."""

    async def __call__(self, handler, event: CallbackQuery, data: dict):
        if not event.from_user or event.data == "sub_check":
            return await handler(event, data)
        conn_id = getattr(event, "business_connection_id", None)
        if not conn_id and event.message:
            conn_id = getattr(event.message, "business_connection_id", None)
        chat_type = getattr(getattr(event, "message", None), "chat", None)
        chat_type = getattr(chat_type, "type", None) if chat_type else None
        if conn_id:
            owner_id = await _get_owner_id_cached(conn_id, "gate_cb")
            if owner_id is None:
                return await handler(event, data)
            if await check_subscription(owner_id):
                return await handler(event, data)
            try:
                await event.answer(
                    "🔒 Автоматизация приостановлена: владелец не подписан на канал",
                    show_alert=True,
                )
            except Exception:
                pass
            return None
        if chat_type in ("group", "supergroup", "channel"):
            return await handler(event, data)  # группы — по решению владельца вне гейта
        if await check_subscription(event.from_user.id):
            return await handler(event, data)
        try:
            await event.answer("🔒 Доступ только для подписчиков канала", show_alert=True)
        except Exception:
            pass
        return None


dp.message.outer_middleware(_SubGateDM())
dp.business_message.outer_middleware(_SubGateBiz())
dp.edited_business_message.outer_middleware(_SubGateBiz())
dp.deleted_business_messages.outer_middleware(_SubGateBiz())
dp.callback_query.outer_middleware(_SubGateCB())
