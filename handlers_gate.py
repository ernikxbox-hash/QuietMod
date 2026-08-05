"""🛡 Гейт подписки: доступ к боту — только подписчикам канала.

Как работает:
- /start и любое сообщение в ЛС с ботом: если пользователь не подписан,
  вместо функций приходит экран «Подпишись на канал» с двумя кнопками:
  🔔 Подписаться (URL) и ✅ Проверить.
- Кнопка «Проверить» делает СВЕЖУЮ проверку через getChatMember и при
  успехе открывает главное меню.
- Проверка ВСЕГДА свежая (кэша нет): вышел из канала → следующее же
  действие блокируется. Единственное исключение — ошибка Telegram API:
  её результат кэшируется на 30 секунд, чтобы не долбить API во время сбоя.
- Бизнес-автоматизация (перехват, архив, уведомления) работает, только
  если ВЛАДЕЛЕЦ подключения подписан на канал. Нет подписки — сообщения
  не сохраняются и не пересылаются (владельцу уходит напоминание).
- Кнопки (callback) тоже под гейтом — кроме самой «Проверить». Кнопки
  в группах и бизнес-кнопки подписанного владельца не трогаем.
- Ошибки Telegram API (бот не добавлен в канал, сеть) НЕ блокируют бота:
  гейт временно открывается. Лог ошибки пишется не чаще раза в 5 минут,
  а при старте админу приходит уведомление, если бота нет в канале.
"""
import time
from html import escape as html_escape
from typing import Optional

from aiogram import BaseMiddleware, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from business_api import _get_owner_id_cached
from core import ADMIN_ID, BOT_USERNAME, CHANNEL_URL, CHANNEL_USERNAME, bot, dp, log
from functions import LINE, home_msg, home_text_for, kb_main

_SUB_ERR_TTL_SECONDS = 30      # негативный кэш ошибки API: повторно не проверяем 30 сек
_DM_PROMPT_SECONDS = 60        # анти-спам: экран «подпишись» не чаще раза в минуту
_BIZ_NOTIFY_SECONDS = 10 * 60  # анти-спам: напоминание владельцу не чаще раза в 10 минут
_WARN_LOG_INTERVAL_SECONDS = 5 * 60  # лог ошибок API не чаще раза в 5 минут

_SUB_ERR_CACHE: dict[int, float] = {}   # user_id -> время последней ОШИБКИ API (гейт открыт)
_DM_PROMPT_LAST: dict[int, float] = {}  # user_id -> время последнего экрана гейта
_BIZ_NOTIFY_LAST: dict[int, float] = {}  # owner_id -> время последнего напоминания
_WARN_LOG_LAST: dict[str, float] = {}   # ключ -> время последнего предупреждения
_CHANNEL_ID: Optional[int] = None       # числовой ID канала (кэш после первого успешного разрешения)


def _warn_throttled(key: str, text: str):
    """Лог ошибки API не чаще раза в 5 минут — иначе каждая проверка спамит в логи."""
    now = time.monotonic()
    if now - _WARN_LOG_LAST.get(key, 0.0) < _WARN_LOG_INTERVAL_SECONDS:
        return
    _WARN_LOG_LAST[key] = now
    log.warning(text)


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


async def _resolve_channel() -> Optional[int]:
    """Числовой ID канала по юзернейму (с кэшем). None — канал недоступен.

    getChat по юзернейму проходит только когда бот уже в канале. Бота
    добавили позже — следующий вызов разрешит канал сам, без перезапуска.
    """
    global _CHANNEL_ID
    if _CHANNEL_ID is not None:
        return _CHANNEL_ID
    if not CHANNEL_USERNAME.strip():
        return None
    try:
        chat = await bot.get_chat(CHANNEL_USERNAME)
        _CHANNEL_ID = chat.id
        log.info(f"🛡 Канал найден: «{getattr(chat, 'title', '?')}» (ID: {_CHANNEL_ID})")
        return _CHANNEL_ID
    except Exception as e:
        _warn_throttled("resolve", f"🛡 Канал @{CHANNEL_USERNAME} не найден: {str(e)[:120]}")
        return None


async def _api_subscribed(uid: int) -> Optional[bool]:
    """Свежая проверка через Telegram API.

    True — подписан, False — не подписан, None — проверить не удалось
    (бот не в канале / сеть / гейт выключен). None → гейт временно открыт.
    """
    if not CHANNEL_USERNAME.strip():
        return None
    chat_id = await _resolve_channel()
    if chat_id is None:
        return None  # канал недоступен → fail-open (лог в _resolve_channel)
    try:
        member = await bot.get_chat_member(chat_id, uid)
    except Exception as e:
        global _CHANNEL_ID
        _CHANNEL_ID = None  # бота могли удалить из канала — разрешим канал заново
        _warn_throttled("sub_api", f"🛡 getChatMember uid={uid}: {str(e)[:120]} — гейт временно открыт")
        return None
    status = member.status
    if status in ("creator", "administrator", "member"):
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def check_subscription_status(uid: int, fresh: bool = False) -> Optional[bool]:
    """Три состояния: True — подписан, False — нет, None — не удалось проверить.

    Проверка ВСЕГДА свежая (без кэша): вышел из канала → следующее же
    действие блокируется. При ошибке API результат кэшируется на 30 секунд
    (гейт открыт), чтобы не долбить Telegram во время сбоя. Кнопка
    «Проверить» (fresh=True) всегда идёт в API.
    """
    now = time.monotonic()
    if not fresh and now - _SUB_ERR_CACHE.get(uid, 0.0) < _SUB_ERR_TTL_SECONDS:
        return None
    ok = await _api_subscribed(uid)
    if ok is False:
        _SUB_ERR_CACHE.pop(uid, None)
        return False
    if ok is True:
        return True
    _SUB_ERR_CACHE[uid] = now
    if len(_SUB_ERR_CACHE) > 5000:
        _SUB_ERR_CACHE.clear()
    return None


async def check_subscription(uid: int, fresh: bool = False) -> bool:
    """Bool-обёртка для middleware и /start: None (ошибка API) = доступ открыт."""
    return (await check_subscription_status(uid, fresh=fresh)) is not False


async def _notify_admin_gate_misconfig(reason: str):
    """DM админу: гейт не работает из-за настроек (бот не в канале)."""
    try:
        await bot.send_message(
            ADMIN_ID,
            "⚠️ <b>Гейт подписки НЕ работает</b>\n\n"
            f"◇ {reason}\n\n"
            "Как исправить (1 минута):\n"
            f"1️⃣ Открой канал @{CHANNEL_USERNAME} → <b>Управление каналом</b>\n"
            "2️⃣ <b>Администраторы</b> → <b>Добавить администратора</b>\n"
            f"3️⃣ Найди @{BOT_USERNAME} → добавь (права — любые)\n\n"
            "После этого гейт заработает сразу, без перезапуска.",
        )
    except Exception:
        pass


async def startup_check():
    """Стартовая самопроверка гейта: лог статуса + DM админу при проблеме."""
    if not CHANNEL_USERNAME.strip():
        log.info("🛡 Гейт подписки ВЫКЛЮЧЕН — CHANNEL_USERNAME не задан")
        return
    chat_id = await _resolve_channel()
    if chat_id is None:
        log.warning(
            f"🛡 Гейт подписки: канал @{CHANNEL_USERNAME} не найден — бот не добавлен"
            " в канал (админом) или юзернейм неверный"
        )
        await _notify_admin_gate_misconfig("канал не найден — бот не добавлен в канал")
        return
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
        log.info(f"🛡 Гейт подписки: @{CHANNEL_USERNAME} — бот в канале ({me.status})")
    except Exception as e:
        global _CHANNEL_ID
        _CHANNEL_ID = None
        log.warning(f"🛡 Гейт подписки: бот НЕ в канале @{CHANNEL_USERNAME}: {str(e)[:120]}")
        await _notify_admin_gate_misconfig("бот не найден в канале")


@dp.message(Command("gate"))
async def cmd_gate(msg: Message):
    """Админ-диагностика гейта: .gate — статус; .gate <ID|@user> — живая проверка юзера."""
    if not msg.from_user or msg.from_user.id != ADMIN_ID:
        return
    lines = [f"🛡 <b>Гейт подписки</b>\n{LINE}"]
    if not CHANNEL_USERNAME.strip():
        lines.append("◇ Канал: <b>не задан</b> (CHANNEL_USERNAME) — гейт выключен")
    else:
        lines.append(f"◇ Канал: @{CHANNEL_USERNAME}")
        lines.append(f"◇ Ссылка: {CHANNEL_URL}")
        chat_id = await _resolve_channel()
        if chat_id is not None:
            lines.append(f"◇ Бот в канале: <b>да</b> (ID: {chat_id})")
            try:
                me = await bot.get_chat_member(chat_id, bot.id)
                lines.append(f"◇ Статус бота: {me.status}")
            except Exception:
                pass
        else:
            lines.append("◇ Бот в канале: <b>НЕТ</b> — добавь бота администратором")
    parts = (msg.text or "").split()
    if len(parts) > 1:
        target = parts[1].lstrip("@")
        uid: Optional[int] = None
        if target.isdigit():
            uid = int(target)
        else:
            try:
                chat = await bot.get_chat(target)
                uid = chat.id
            except Exception:
                pass
        if uid is None:
            lines.append(f"◇ Юзер <code>{target}</code> не найден — укажи числовой ID")
        else:
            status = await check_subscription_status(uid, fresh=True)
            if status is True:
                verdict = "✅ подписан — доступ есть"
            elif status is False:
                verdict = "❌ НЕ подписан — доступ закрыт"
            else:
                verdict = "⚠️ проверить не удалось (сбой API) — доступ открыт"
            lines.append(f"◇ Проверка <code>{html_escape(target)}</code> (ID {uid}): {verdict}")
    else:
        lines.append(f"{LINE}\n◇ <code>.gate 1933015948</code> — проверить конкретного юзера")
    try:
        await msg.answer("\n".join(lines))
    except Exception as e:
        log.warning(f"🛡 .gate: {e}")


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
