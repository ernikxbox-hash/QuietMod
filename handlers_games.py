"""⚔️ Камень · Ножницы · Бумага (.knb) — написана с нуля.

Надёжная схема:
- Сообщение с командой .knb удаляется, игровое поле/вызов отправляется
  НОВЫМ сообщением (его message_id хранится в состоянии игры).
- Всё дальнейшее — через inline-кнопки: ходы, реванш, отмена.
- Ключ игры: (mode, conn_id, chat_id) для бизнес-чатов, (group, chat_id)
  для обычных групп. Ключ одинаково строится и в хендлере команды,
  и в callback'ах — коллизий нет.
- Старая незавершённая игра в том же чате автоматически закрывается,
  когда начинается новая. Протухшие игры (15 минут) подчищаются.
"""
import asyncio
import random
import re
import time
from typing import Optional

from aiogram import F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from html import escape as html_escape

import database as db
from business_api import (
    _business_delete_message_ex,
    _business_edit_message,
    _business_send_message_ex,
    _get_owner_id_cached,
)
from core import bot, dp, log
from functions import LINE, knb_games

_KPB_EMOJI = {"r": "✊", "s": "✌️", "p": "🖐"}
_KPB_BEATS = {"r": "s", "s": "p", "p": "r"}
_KNB_STALE_SECONDS = 15 * 60

_GROUP_MEMBERS: dict[int, dict[int, dict]] = {}  # chat_id -> {user_id: {id, username, full_name}}


# ── Утилиты ────────────────────────────────────────────────────────────
def _knb_game_alive(game: dict) -> bool:
    """True, если игра/вызов ещё актуальна (не протухла за 15 минут)."""
    ts = game.get("ts", 0) or 0
    if ts <= 0:
        return False
    return (time.monotonic() - ts) < _KNB_STALE_SECONDS


def _knb_forget_stale() -> None:
    """Подчищает протухшие игры из памяти."""
    now = time.monotonic()
    stale = [
        k for k, g in knb_games.items()
        if (g.get("ts", 0) or 0) <= 0 or (now - (g.get("ts", 0) or 0)) >= _KNB_STALE_SECONDS
    ]
    for k in stale:
        knb_games.pop(k, None)


def _knb_cache_member(chat_id: int, user) -> None:
    if not user or not getattr(user, "id", None):
        return
    members = _GROUP_MEMBERS.setdefault(chat_id, {})
    members[user.id] = {
        "id": user.id,
        "username": user.username or "",
        "full_name": user.full_name or "",
    }


def _knb_display_name(name: str, username: str) -> str:
    if username:
        return f"@{username}"
    return html_escape(name) or "Собеседник"


def _knb_header(game: dict) -> str:
    head = (
        f"⚔️ <b>КАМЕНЬ · НОЖНИЦЫ · БУМАГА</b>\n"
        f"<code>{LINE}</code>\n\n"
        f"◇ {game['a_name']}  <b>vs</b>  {game['b_name']}\n\n"
    )
    sa = game.get("score_a", 0)
    sb = game.get("score_b", 0)
    if sa or sb:
        head += f"📊 Счёт: {game['a_name']}  <b>{sa}:{sb}</b>  {game['b_name']}\n\n"
    return head


def _knb_challenge_text(game: dict) -> str:
    a_link = f"<a href=\"tg://user?id={game['a_id']}\">{game['a_name']}</a>"
    b_link = f"<a href=\"tg://user?id={game['b_id']}\">{game['b_name']}</a>"
    return (
        "⚔️ <b>ВЫЗОВ НА БОЙ!</b>\n"
        f"<code>{LINE}</code>\n\n"
        f"◇ {a_link}  бросил(а) вызов  {b_link}\n"
        "◇ Камень · Ножницы · Бумага — 1 на 1\n\n"
        f"<code>{LINE}</code>\n"
        f"◇ {b_link}, жми <b>«⚔️ Принять бой»</b> 👇"
    )


def _knb_move_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✊", callback_data="knb_r"),
            InlineKeyboardButton(text="✌️", callback_data="knb_s"),
            InlineKeyboardButton(text="🖐", callback_data="knb_p"),
        ],
        [InlineKeyboardButton(text="✕ Отменить", callback_data="knb_cancel")],
    ])


def _knb_result_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Сыграть ещё", callback_data="knb_again")],
        [InlineKeyboardButton(text="✕ Закрыть", callback_data="knb_cancel")],
    ])


def _knb_challenge_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ Принять бой", callback_data="knb_accept")],
        [InlineKeyboardButton(text="✕ Отменить", callback_data="knb_cancel")],
    ])


def _knb_kb_dump(markup) -> dict:
    """Сериализует InlineKeyboardMarkup в простой dict для сырого Telegram API."""
    if isinstance(markup, InlineKeyboardMarkup):
        return markup.model_dump(exclude_none=True)
    return markup or {"inline_keyboard": []}


def _knb_key_from_call(call: CallbackQuery) -> Optional[tuple]:
    """Ключ игры из callback'а — тот же, что и в хендлере команды."""
    if not call.message or not call.message.chat:
        return None
    conn_id = getattr(call, "business_connection_id", None)
    if not conn_id and call.message:
        conn_id = getattr(call.message, "business_connection_id", None)
    chat_id = call.message.chat.id
    if conn_id:
        prefix = "dm" if getattr(call.message.chat, "type", None) == "private" else "bg"
        return (prefix, conn_id, chat_id)
    return ("group", chat_id)


async def _knb_edit(game: dict, msg_id: int, text: str, reply_markup=None) -> bool:
    """Обновляет сообщение игры (после хода, реванша, отмены)."""
    if game.get("conn_id"):
        return await _business_edit_message(game["conn_id"], game["chat_id"], msg_id, text, reply_markup=reply_markup)
    try:
        await bot.edit_message_text(
            text,
            chat_id=game["chat_id"],
            message_id=msg_id,
            reply_markup=reply_markup,
        )
        return True
    except Exception as e:
        log.warning(f"knb edit: {e}")
        return False


async def _knb_delete_command(msg: Message, conn_id: Optional[str]):
    """Удаляет сообщение с командой .knb (в бизнес-чатах и группах)."""
    if conn_id:
        try:
            await _business_delete_message_ex(conn_id, msg.message_id)
        except Exception:
            pass
    else:
        try:
            await bot.delete_message(msg.chat.id, msg.message_id)
        except Exception:
            pass


async def _knb_resolve_target(msg: Message) -> Optional[dict]:
    """Определяет, кого вызывают на бой: mention-сущность, @username из кэша или reply."""
    for ent in (msg.entities or []):
        if ent.type == "text_mention" and ent.user:
            u = ent.user
            return {"id": u.id, "full_name": u.full_name or "", "username": u.username or ""}
    m = re.match(r"(?i)^\.knb\s+@?([a-zA-Z0-9_]{3,32})$", (msg.text or "").strip())
    if m:
        uname = m.group(1).lower()
        for info in _GROUP_MEMBERS.get(msg.chat.id, {}).values():
            if (info.get("username") or "").lower() == uname:
                return dict(info)
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return {"id": u.id, "full_name": u.full_name or "", "username": u.username or ""}
    return None


# ── Старт игры: ЛС (Business) — владелец vs собеседник ───────────────
@dp.business_message(F.text.regexp(r"(?i)^\.knb(\s+.*)?$"))
async def on_knb_start(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return
    owner_id = await _get_owner_id_cached(conn_id, ".knb")
    if owner_id is None:
        return
    chat_type = getattr(msg.chat, "type", None)
    if chat_type == "private":
        if not msg.from_user or msg.from_user.id != owner_id:
            return
        _knb_forget_stale()
        key = ("dm", conn_id, msg.chat.id)
        if key in knb_games:
            knb_games.pop(key, None)  # старую игру закрываем, начинаем новую
        game = {
            "mode": "dm",
            "chat_id": msg.chat.id,
            "conn_id": conn_id,
            "a_id": owner_id,
            "b_id": msg.chat.id,
            "a_name": _knb_display_name(msg.from_user.full_name or "", msg.from_user.username or ""),
            "b_name": _knb_display_name(
                getattr(msg.chat, "first_name", "") or "",
                getattr(msg.chat, "username", "") or "",
            ),
            "status": "playing",
            "turn": random.choice(["a", "b"]),
            "move_a": None,
            "move_b": None,
            "score_a": 0,
            "score_b": 0,
            "ts": time.monotonic(),
        }
        knb_games[key] = game
        first_name = game["a_name"] if game["turn"] == "a" else game["b_name"]
        text = _knb_header(game) + f"🎲 <b>Первый начинает:</b> {first_name}"
        ok, retry_after, _ = await _business_send_message_ex(
            conn_id, msg.chat.id, text, reply_markup=_knb_kb_dump(_knb_move_kb()), parse_mode="HTML",
        )
        if not ok and retry_after:
            await asyncio.sleep(int(retry_after))
            ok, _, _ = await _business_send_message_ex(
                conn_id, msg.chat.id, text, reply_markup=_knb_kb_dump(_knb_move_kb()), parse_mode="HTML",
            )
        if not ok:
            knb_games.pop(key, None)  # не смогли отправить поле — не держим игру
            log.warning(f"⚔️ .knb send board failed conn={conn_id} chat={msg.chat.id}")
            return
        await _knb_delete_command(msg, conn_id)
        log.info(f"⚔️ .knb start conn={conn_id} chat={msg.chat.id} first={game['turn']}")
        return

    if chat_type not in ("group", "supergroup"):
        return
    # ── Группа через Business: вызов .knb @user ──
    if not msg.from_user:
        return
    _knb_cache_member(msg.chat.id, msg.from_user)
    _knb_forget_stale()
    key = ("bg", conn_id, msg.chat.id)
    if key in knb_games:
        knb_games.pop(key, None)
    target = await _knb_resolve_target(msg)
    if target is None:
        await _business_send_message_ex(
            conn_id, msg.chat.id,
            "⚔️ <b>Не нашёл, кого ты вызываешь.</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Напиши так: <code>.knb @username</code>\n"
            "◇ Или <b>ответь на сообщение</b> человека и напиши <code>.knb</code>",
        )
        return
    if target["id"] == msg.from_user.id:
        await _business_send_message_ex(conn_id, msg.chat.id, "⚔️ Нельзя бросить вызов самому себе 😅")
        return
    game = {
        "mode": "group",
        "chat_id": msg.chat.id,
        "conn_id": conn_id,
        "a_id": msg.from_user.id,
        "b_id": target["id"],
        "a_name": _knb_display_name(msg.from_user.full_name or "", msg.from_user.username or ""),
        "b_name": _knb_display_name(target["full_name"], target["username"]),
        "status": "challenge",
        "turn": None,
        "move_a": None,
        "move_b": None,
        "score_a": 0,
        "score_b": 0,
        "ts": time.monotonic(),
    }
    knb_games[key] = game
    ok, retry_after, _ = await _business_send_message_ex(
        conn_id, msg.chat.id, _knb_challenge_text(game),
        reply_markup=_knb_kb_dump(_knb_challenge_kb()), parse_mode="HTML",
    )
    if not ok and retry_after:
        await asyncio.sleep(int(retry_after))
        ok, _, _ = await _business_send_message_ex(
            conn_id, msg.chat.id, _knb_challenge_text(game),
            reply_markup=_knb_kb_dump(_knb_challenge_kb()), parse_mode="HTML",
        )
    if not ok:
        knb_games.pop(key, None)
        log.warning(f"⚔️ .knb challenge send failed conn={conn_id} chat={msg.chat.id}")
        return
    await _knb_delete_command(msg, conn_id)
    log.info(f"⚔️ .knb challenge bg conn={conn_id} chat={msg.chat.id} by={msg.from_user.id} target={target['id']}")


# ── Вызов в группе (обычный бот) ─────────────────────────────────────
@dp.message(F.text.regexp(r"(?i)^\.knb(\s+.*)?$"), F.chat.type.in_(("group", "supergroup")))
async def on_knb_challenge(msg: Message):
    """Группа (обычный бот): .knb @user — вызов на бой 1×1, все видят."""
    if not msg.from_user:
        return
    chat_id = msg.chat.id
    uid = msg.from_user.id
    _knb_cache_member(chat_id, msg.from_user)
    await db.upsert_user(uid, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(chat_id, msg.chat.title or "", msg.chat.type)
    _knb_forget_stale()
    key = ("group", chat_id)
    if key in knb_games:
        knb_games.pop(key, None)
    target = await _knb_resolve_target(msg)
    if target is None:
        await msg.reply(
            "⚔️ <b>Не нашёл, кого ты вызываешь.</b>\n"
            f"<code>{LINE}</code>\n\n"
            "◇ Напиши так: <code>.knb @username</code>\n"
            "◇ Или <b>ответь на сообщение</b> человека и напиши <code>.knb</code>"
        )
        return
    if target["id"] == uid:
        await msg.reply("⚔️ Нельзя бросить вызов самому себе 😅")
        return
    game = {
        "mode": "group",
        "chat_id": chat_id,
        "conn_id": None,
        "a_id": uid,
        "b_id": target["id"],
        "a_name": _knb_display_name(msg.from_user.full_name or "", msg.from_user.username or ""),
        "b_name": _knb_display_name(target["full_name"], target["username"]),
        "status": "challenge",
        "turn": None,
        "move_a": None,
        "move_b": None,
        "score_a": 0,
        "score_b": 0,
        "ts": time.monotonic(),
    }
    knb_games[key] = game
    await _knb_delete_command(msg, None)
    try:
        sent = await bot.send_message(
            chat_id, _knb_challenge_text(game),
            reply_markup=_knb_challenge_kb(), parse_mode="HTML",
        )
    except Exception as e:
        knb_games.pop(key, None)
        log.warning(f"knb challenge send: {e}")
        return
    game["msg_id"] = sent.message_id
    log.info(f"⚔️ .knb challenge group={chat_id} by={uid} target={target['id']}")


# ── Принять бой ───────────────────────────────────────────────────────
@dp.callback_query(F.data == "knb_accept")
async def cb_knb_accept(call: CallbackQuery):
    key = _knb_key_from_call(call)
    if not key or not call.message:
        await call.answer("⛔ Вызов недоступен", show_alert=True)
        return
    game = knb_games.get(key)
    if not game:
        await call.answer("⚔️ Вызов уже неактуален", show_alert=True)
        return
    if game.get("status") != "challenge":
        await call.answer("⚔️ Бой уже начался", show_alert=False)
        return
    if not _knb_game_alive(game):
        knb_games.pop(key, None)
        await call.answer("⚔️ Вызов протух — брось новый: .knb", show_alert=True)
        return
    uid = call.from_user.id
    if uid != game["b_id"]:
        await call.answer("🙅 Этот вызов не для тебя!", show_alert=False)
        return
    game["status"] = "playing"
    game["turn"] = random.choice(["a", "b"])
    _knb_cache_member(game["chat_id"], call.from_user)
    first_name = game["a_name"] if game["turn"] == "a" else game["b_name"]
    await call.answer("⚔️ Бой принят! 🥊", show_alert=False)
    await _knb_edit(
        game, call.message.message_id,
        _knb_header(game) + f"🎲 <b>Первый начинает:</b> {first_name}",
        _knb_move_kb(),
    )


# ── Ход ───────────────────────────────────────────────────────────────
@dp.callback_query(F.data.in_({"knb_r", "knb_s", "knb_p"}))
async def cb_knb_move(call: CallbackQuery):
    key = _knb_key_from_call(call)
    if not key or not call.message:
        await call.answer("⛔ Игра недоступна", show_alert=True)
        return
    game = knb_games.get(key)
    if not game or game.get("status") != "playing":
        await call.answer("⚔️ Игра не найдена — начни новую: .knb", show_alert=True)
        return
    uid = call.from_user.id
    move = call.data.split("_")[1]
    expected = game["a_id"] if game["turn"] == "a" else game["b_id"]
    if uid != expected:
        await call.answer("🙅 Не твой ход — жди очереди!", show_alert=False)
        return
    _knb_cache_member(game["chat_id"], call.from_user)
    if game["turn"] == "a":
        game["move_a"] = move
        game["turn"] = "b"
    else:
        game["move_b"] = move
        game["turn"] = "a"
    if game["move_a"] and game["move_b"]:
        emoji_a = _KPB_EMOJI[game["move_a"]]
        emoji_b = _KPB_EMOJI[game["move_b"]]
        if game["move_a"] == game["move_b"]:
            result = "🤝 <b>Ничья!</b>"
        elif _KPB_BEATS[game["move_a"]] == game["move_b"]:
            game["score_a"] = game.get("score_a", 0) + 1
            result = f"🏆 Побеждает: <b>{game['a_name']}</b>"
        else:
            game["score_b"] = game.get("score_b", 0) + 1
            result = f"🏆 Побеждает: <b>{game['b_name']}</b>"
        await _knb_edit(
            game, call.message.message_id,
            _knb_header(game)
            + f"🆚 {game['a_name']} {emoji_a}  vs  {emoji_b} {game['b_name']}\n\n"
            + f"{result}",
            _knb_result_kb(),
        )
        await call.answer("🎉 Итог готов!", show_alert=False)
        return
    mover_name = game["a_name"] if uid == game["a_id"] else game["b_name"]
    next_name = game["a_name"] if game["turn"] == "a" else game["b_name"]
    await _knb_edit(
        game, call.message.message_id,
        _knb_header(game)
        + f"✅ <b>{mover_name}</b> сделал ход 🤫\n\n"
        + f"🎯 Теперь очередь: <b>{next_name}</b>",
        _knb_move_kb(),
    )
    await call.answer("🤫 Ход записан", show_alert=False)


# ── Реванш ────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "knb_again")
async def cb_knb_again(call: CallbackQuery):
    key = _knb_key_from_call(call)
    if not key or not call.message:
        await call.answer("⛔ Игра недоступна", show_alert=True)
        return
    game = knb_games.get(key)
    if not game:
        await call.answer("⚔️ Игра не найдена", show_alert=True)
        return
    uid = call.from_user.id
    if uid not in (game["a_id"], game["b_id"]):
        await call.answer("🙅 Реванш могут назначить только игроки", show_alert=False)
        return
    game["move_a"] = None
    game["move_b"] = None
    game["status"] = "playing"
    game["turn"] = random.choice(["a", "b"])
    first_name = game["a_name"] if game["turn"] == "a" else game["b_name"]
    await _knb_edit(
        game, call.message.message_id,
        _knb_header(game) + f"🎲 <b>Первый начинает:</b> {first_name}",
        _knb_move_kb(),
    )
    await call.answer("🔁 Новый раунд", show_alert=False)


# ── Отмена / закрытие ─────────────────────────────────────────────────
@dp.callback_query(F.data == "knb_cancel")
async def cb_knb_cancel(call: CallbackQuery):
    key = _knb_key_from_call(call)
    if not key or not call.message:
        await call.answer("⛔ Игра недоступна", show_alert=True)
        return
    game = knb_games.get(key)
    if not game:
        await call.answer("⚔️ Игры нет", show_alert=False)
        return
    uid = call.from_user.id
    if game.get("status") == "challenge":
        if uid not in (game["a_id"], game["b_id"]):
            await call.answer("🙅 Этот вызов не для тебя!", show_alert=False)
            return
        close_text = "⚔️ <b>Вызов отклонён</b> — бой отменён 👁️"
    else:
        if uid not in (game["a_id"], game["b_id"]):
            await call.answer("🙅 Закрыть игру могут только игроки", show_alert=False)
            return
        close_text = "⚔️ <b>Игра закрыта</b> — было жарко 👁️"
    knb_games.pop(key, None)
    await _knb_edit(
        game, call.message.message_id, close_text,
        InlineKeyboardMarkup(inline_keyboard=[]),
    )
    await call.answer("⚔️ Игра закрыта", show_alert=False)
