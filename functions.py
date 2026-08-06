import asyncio
import re
from datetime import date, timedelta, timezone
from typing import Optional
import aiohttp
from ddgs import DDGS
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from html import escape as html_escape
import database as db
from core import (
    ADMIN_ID,
    BOT_USERNAME,
    GROQ_API_KEYS,
    GROQ_MODEL,
    bot,
    get_http,
    log,
)
from business_api import _business_edit_message_ex
from mtproto_resolver import resolve_username_mtproto

ai_history: dict[int, list] = {}
spam_tasks: dict[tuple[int, int], asyncio.Task] = {}
business_spam_tasks: dict[tuple[str, int, int], asyncio.Task] = {}
business_muted_chats: set[tuple[str, int]] = set()
business_nomute_chats: set[tuple[str, int]] = set()
business_afk: dict[str, dict] = {}
business_afk_last_reply: dict[tuple[str, int], float] = {}
user_afk: dict[int, dict] = {}  # AFK из ЛС с ботом: user_id -> {owner_id, started_at, note}
knb_games: dict[tuple, dict] = {}  # ("dm"|"bg", conn_id, chat_id) или ("group", chat_id) -> состояние игры .knb
business_code_mode: set[str] = set()
business_wbl_chats: set[tuple[str, int]] = set()
_GROQ_KEY_INDEX: int = 0  # индекс последнего рабочего Groq-ключа (для фолбэка)
PROFANITY_RE = re.compile(
    r"(?iu)\b("
    r"хуй|хуе|хуя|хуи|хуё|хуйн|хуесос|хер|херн|"
    r"пизд|пезд|пизж|"
    r"бля|бляд|блядств|"
    r"еб|ёб|йоб|уеб|уёб|выеб|выёб|заеб|заёб|наеб|наёб|проеб|проёб|"
    r"сука|сучк|сукин|"
    r"мраз|гандон|презерватив|"
    r"пидор|пидр|пидорас|пидорасин|педик|"
    r"ублюд|мудак|мудил|мудозвон|"
    r"чмо|чмош|"
    r"дыряв|тупоголов|нищ|обоссан|оссан|ссан|ссать|"
    r"даун|плешив|дебил|идиот|"
    r"твар|шлюх|проститут|"
    r"дроч|дрочил|дрочер|"
    r"жоп|жопа|жопник|"
    r"говн|гавно|говно|дерьм|"
    r"писюн|писю|"
    r"нахуй|оху|аху|"
    r"xуй|xуе|xуя|xуи|xуё|xуйн|xуесос|huy|hui|xuy|"
    r"pizd|pizda|pizdec|blya|blyad|ebat|eban|zaeb|naeb|uebal|"
    r"fuck|shit|bitch|cunt|asshole|motherfuck|dick|pussy"
    r")\w*\b"
)
_WBL_TRANSLATE = str.maketrans({
    "ё": "е",
    "0": "о",
    "3": "з",
    "@": "а",
    "$": "с",
    "x": "х",
    "y": "у",
    "a": "а",
    "b": "в",
    "c": "с",
    "e": "е",
    "k": "к",
    "m": "м",
    "h": "н",
    "o": "о",
    "p": "р",
    "t": "т",
})
_WBL_SEP_RE = re.compile(r"(?iu)[^0-9a-zа-я]+")
_WBL_SPACE_RE = re.compile(r"\s+")
_WBL_REPEAT_RE = re.compile(r"(?iu)(.)\1{3,}")
_WBL_FLOOD_CHAR_RE = re.compile(r"(?iu)(.)\1{14,}")
_WBL_FLOOD_PUNCT_RE = re.compile(r"(?u)[!?.,:;()\[\]{}<>\-_=+*/\\]{25,}")
last_notify_msg: dict[int, int] = {}
home_msg: dict[int, int] = {}

LINE = "──────────────────"
MSK = timezone(timedelta(hours=3))

def fmt_msg_date(dt) -> str:
    return dt.astimezone(MSK).strftime("%d.%m.%Y · %H:%M")
def ref_link(uid: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
def _wbl_normalize_text(text: str) -> str:
    t = (text or "").lower().translate(_WBL_TRANSLATE)
    t = _WBL_SEP_RE.sub(" ", t)
    t = _WBL_SPACE_RE.sub(" ", t).strip()
    return t
def _wbl_deobfuscate(norm_text: str) -> str:
    toks = norm_text.split()
    out: list[str] = []
    buf: list[str] = []
    for tok in toks:
        if tok.isalpha() and len(tok) <= 2:
            buf.append(tok)
            continue
        if buf:
            if len(buf) >= 2 and any(len(x) == 1 for x in buf):
                out.append("".join(buf))
            else:
                out.extend(buf)
            buf = []
        out.append(tok)
    if buf:
        if len(buf) >= 2 and any(len(x) == 1 for x in buf):
            out.append("".join(buf))
        else:
            out.extend(buf)
    return " ".join(out)
def _wbl_squeeze_repeats(text: str) -> str:
    return _WBL_REPEAT_RE.sub(lambda m: m.group(1) * 2, text)
def _contains_profanity(text: str) -> bool:
    if not text:
        return False
    if PROFANITY_RE.search(text):
        return True
    norm = _wbl_normalize_text(text)
    if not norm:
        return False
    if PROFANITY_RE.search(norm):
        return True
    deob = _wbl_squeeze_repeats(_wbl_deobfuscate(norm))
    return bool(PROFANITY_RE.search(deob))
def _wbl_looks_like_flood(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 40:
        return False
    if _WBL_FLOOD_CHAR_RE.search(t):
        return True
    if _WBL_FLOOD_PUNCT_RE.search(t):
        return True
    compact = _WBL_SPACE_RE.sub("", t)
    if len(compact) >= 600:
        uniq = len(set(compact))
        return (uniq / max(1, len(compact))) <= 0.15
    return False
def _wbl_should_delete(text: str) -> bool:
    if not text:
        return False
    if _contains_profanity(text):
        return True
    return _wbl_looks_like_flood(text)
MEDIA_MAP = {
    "photo":      "◆ Фото",
    "video":      "◆ Видео",
    "audio":      "◆ Аудио",
    "voice":      "◆ Голосовое",
    "document":   "◆ Документ",
    "sticker":    "◆ Стикер",
    "video_note": "◆ Кружок",
    "animation":  "◆ GIF",
}

_PRICE_BASE_USD = 14
_PRICE_RANK_PRICE = {
    1: 1, 2: 2, 3: 4, 4: 8, 5: 15,
    6: 30, 7: 60, 8: 120, 9: 250, 10: 500,
}
_PRICE_VOWELS = set("aeiouyаеёиоуыэюяAEIOUYАЕЁИОУЫЭЮЯ")

def _price_is_pronounceable(username: str) -> bool:
    letters = [c for c in username if c.isalpha()]
    if not letters:
        return False
    vowels = sum(1 for c in letters if c in _PRICE_VOWELS)
    return 1 <= vowels <= len(letters) - 1

def _price_rank(username: str) -> int:
    n = len(username)
    if n <= 2:
        rank = 10
    elif n == 3:
        rank = 9
    elif n == 4:
        rank = 8
    elif n == 5:
        rank = 6
    elif n == 6:
        rank = 5
    elif n == 7:
        rank = 3
    else:
        rank = 1
    if any(c.isdigit() for c in username):
        rank -= 2
    if "_" in username:
        rank -= 3
    if not _price_is_pronounceable(username):
        rank -= 1
    return max(1, min(10, rank))

def _price_stars(rank: int) -> str:
    if rank >= 9:
        return "⭐⭐⭐⭐⭐"
    if rank >= 7:
        return "⭐⭐⭐⭐☆"
    if rank >= 5:
        return "⭐⭐⭐☆☆"
    if rank >= 3:
        return "⭐⭐☆☆☆"
    return "⭐☆☆☆☆"

def _price_estimate(username: str) -> Optional[str]:
    """Оценка стоимости юзернейма (эвристика в стиле Fragment)."""
    u = (username or "").strip().lstrip("@").strip()
    if not u or not re.fullmatch(r"[a-zA-Z0-9_]{1,32}", u):
        return None
    n = len(u)
    has_digits = any(c.isdigit() for c in u)
    has_underscore = "_" in u
    pronounceable = _price_is_pronounceable(u)
    rank = _price_rank(u)
    base = _PRICE_BASE_USD * _PRICE_RANK_PRICE[rank]
    if rank == 1:
        low = high = _PRICE_BASE_USD
    else:
        low = max(_PRICE_BASE_USD, int(base * 0.9))
        high = int(base * 1.1)
    bars = "▰" * rank + "▱" * (10 - rank)
    adv = []
    if not has_digits:
        adv.append("🔤 Без цифр")
    if not has_underscore:
        adv.append("✨ Без подчёркивания")
    if pronounceable:
        adv.append("🗣 Хорошая произносимость")
    if n <= 4:
        adv.append(f"📏 Короткий ({n} симв.)")
    if not adv:
        adv.append("—")
    dis = []
    if n >= 7:
        dis.append("📐 Длинноватый (7+ символов)")
    if has_digits:
        dis.append("🔢 Содержит цифры")
    if has_underscore:
        dis.append("✨ Содержит подчёркивание")
    if not pronounceable:
        dis.append("🗣 Трудно произнести")
    dis.append("❓ Нет в базе Fragment — спрос не подтверждён рынком")
    safe = html_escape(u)
    return (
        "📊 Статус на Fragment\n"
        "❌ Продаж на Fragment не обнаружено\n\n"
        f"📈 Оценка юзернейма <a href=\"https://t.me/{safe}\">@{safe}</a> (https://t.me/{safe})\n\n"
        f"💰 Ориентировочная стоимость: <b>${low} — ${high}</b>\n"
        f"🏷 Стоимость создания: 10 GRAM (TON) (~${_PRICE_BASE_USD})\n"
        f"🏆 Ранг: <code>{bars}</code> {rank}/10\n"
        f"⭐️ Потенциал: {_price_stars(rank)}\n\n"
        "✅ Преимущества:\n" + "\n".join(adv) + "\n\n"
        "❌ Недостатки:\n" + "\n".join(dis) + "\n\n"
        f"— 👁️ @{BOT_USERNAME}"
    )

def fmt_sender(from_name: str, username: str) -> str:
    if username:
        return f"{from_name} ({username})"
    return from_name
async def resolve_username_to_chat(username: str) -> Optional[dict]:
    """@username → данные чата/пользователя (id, full_name, username, bio).

    Bot API по юзернейму умеет находить только каналы и супергруппы —
    людей так искать нельзя. Поэтому для людей сначала ищем их ID в
    собственной базе (архив бизнес-сообщений, users, sled_targets), а
    если человека бот никогда не видел — пробуем MTProto-резолвер
    (опция, TELEGRAM_API_ID/HASH). По ID дальше работают getChat и
    getUserProfilePhotos.
    """
    s = (username or "").lstrip("@").strip()
    if not s:
        return None
    # 1) Знакомые люди — ID из собственной базы: без лишних запросов к API
    target_id = await db.find_sender_id_by_username(s)
    if target_id is not None:
        try:
            chat = await bot.get_chat(target_id)
            return {
                "id": chat.id,
                "full_name": getattr(chat, "full_name", None) or getattr(chat, "title", "") or "",
                "username": (getattr(chat, "username", "") or "").lstrip("@"),
                "bio": getattr(chat, "bio", "") or "",
            }
        except Exception as e:
            log.debug(f"resolve_username_to_chat: getChat(id={target_id}): {e}")
    # 2) Каналы и супергруппы — родной getChat по юзернейму. Для юзеров
    #    Telegram всегда отвечает 400, поэтому этот шаг идёт ПОСЛЕ базы
    try:
        chat = await bot.get_chat(s)
        return {
            "id": chat.id,
            "full_name": getattr(chat, "full_name", None) or getattr(chat, "title", "") or "",
            "username": (getattr(chat, "username", "") or "").lstrip("@"),
            "bio": getattr(chat, "bio", "") or "",
        }
    except Exception:
        pass
    # 3) Любой публичный юзер — MTProto-резолвер (если настроен)
    return await resolve_username_mtproto(s)

def home_text() -> str:
    return (
        f"◆ <b>QUIET MOD</b> 👁️\n"
        f"<code>{LINE}</code>\n\n"
        f"◇ Статус       <b>Свободен · без лимитов</b>\n"
        f"◇ Перехват     <b>безлимит</b>\n"
        f"◇ Архив        <b>безлимит</b>\n"
        f"◇ Поиск        <b>включён</b>\n"
        f"◇ ИИ           <b>без лимитов</b>\n"
        f"<code>{LINE}</code>"
    )
def home_text_for(uid: int, name: str) -> str:
    """Полное приветствие для конкретного пользователя (после прохода гейта подписки)."""
    return (
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
async def _show_home(uid: int, text: str, reply_markup, target_msg: "Message | None" = None):
    existing_id = home_msg.get(uid)
    if existing_id and target_msg:
        try:
            await bot.edit_message_text(
                text, chat_id=uid, message_id=existing_id,
                reply_markup=reply_markup, parse_mode="HTML"
            )
            return
        except Exception:
            pass
    if target_msg:
        sent = await target_msg.answer(text, reply_markup=reply_markup)
    else:
        sent = await bot.send_message(uid, text, reply_markup=reply_markup)
    home_msg[uid] = sent.message_id
async def _delete_notify_quiet(owner_id: int, msg_id: int):
    try:
        await bot.delete_message(owner_id, msg_id)
    except Exception:
        pass

async def _send_notify(owner_id: int, text: str, reply_markup=None) -> Optional[int]:
    old_id = last_notify_msg.get(owner_id)
    if old_id:
        last_notify_msg.pop(owner_id, None)
        asyncio.create_task(_delete_notify_quiet(owner_id, old_id))
    try:
        sent = await bot.send_message(owner_id, text, reply_markup=reply_markup)
        last_notify_msg[owner_id] = sent.message_id
        return sent.message_id
    except Exception as e:
        log.error(f"send notify to owner={owner_id}: {e}")
        return None
# Кастомные эмодзи (пак CPT_Emoji / @Cryptomus EMOJI) для кнопок главного меню.
# ID получены через команду .emoji CPT_Emoji. Пустая строка = без иконки.
# На кнопках только премиум-иконки (icon_custom_emoji_id) — старые обычные
# эмодзи из текста кнопок убраны.
CUSTOM_EMOJI_PROFILE   = "5346136537123801643"  # Профиль
CUSTOM_EMOJI_HOWTO     = "5348292765325212780"  # Подключение
CUSTOM_EMOJI_SEARCH    = "5345840270279724328"  # Поиск по архиву
CUSTOM_EMOJI_DONATE    = "5348356047373354143"  # Поддержать проект
CUSTOM_EMOJI_AI        = "5346024644635804737"  # ИИ-консьерж
CUSTOM_EMOJI_REFERRALS = "5348348681504441752"  # Приглашения
CUSTOM_EMOJI_SUGGEST   = "5348417723103722255"  # Предложить
CUSTOM_EMOJI_ARCHIVE   = "5346267671065281783"  # Архив
CUSTOM_EMOJI_SAVED     = "5348178055338671586"  # Сохранённые
CUSTOM_EMOJI_CLEAR     = "5345809410939700735"  # Очистить
CUSTOM_EMOJI_ADMIN     = "5348129380474306311"  # Admin Suite


def kb_main(uid: int) -> InlineKeyboardMarkup:
    rows = []
    if uid == ADMIN_ID:
        rows.append([
            InlineKeyboardButton(
                text="Admin Suite",
                callback_data="adm",
                icon_custom_emoji_id=CUSTOM_EMOJI_ADMIN or None,
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="Архив",
            callback_data="show_all",
            icon_custom_emoji_id=CUSTOM_EMOJI_ARCHIVE or None,
        ),
        InlineKeyboardButton(
            text="Профиль",
            callback_data="stats",
            icon_custom_emoji_id=CUSTOM_EMOJI_PROFILE or None,
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            text="Сохранённые",
            callback_data="show_saved",
            icon_custom_emoji_id=CUSTOM_EMOJI_SAVED or None,
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            text="Поиск по архиву",
            callback_data="search",
            icon_custom_emoji_id=CUSTOM_EMOJI_SEARCH or None,
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="ИИ-консьерж — без лимитов",
            callback_data="ai_open",
            icon_custom_emoji_id=CUSTOM_EMOJI_AI or None,
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="Приглашения",
            callback_data="referrals",
            icon_custom_emoji_id=CUSTOM_EMOJI_REFERRALS or None,
        ),
        InlineKeyboardButton(
            text="Очистить",
            callback_data="clear_cache",
            icon_custom_emoji_id=CUSTOM_EMOJI_CLEAR or None,
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            text="Предложить",
            callback_data="suggest_idea",
            icon_custom_emoji_id=CUSTOM_EMOJI_SUGGEST or None,
        ),
        InlineKeyboardButton(
            text="Подключение",
            callback_data="howto",
            icon_custom_emoji_id=CUSTOM_EMOJI_HOWTO or None,
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            text="Поддержать проект",
            callback_data="donate",
            icon_custom_emoji_id=CUSTOM_EMOJI_DONATE or None,
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
def kb_back(target: str = "menu", label: str = "← В меню") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"back_{target}")]
    ])
def kb_notify(save_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◆ Сохранить ➩", callback_data=f"nsave_{save_id}"),
            InlineKeyboardButton(text="✕ Удалить",      callback_data=f"ndel_{save_id}"),
        ],
    ])
def kb_ai() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✕ Сбросить диалог", callback_data="ai_clear"),
            InlineKeyboardButton(text="← Завершить",       callback_data="ai_exit"),
        ],
    ])
def kb_donate() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⟡ 15 ⭐", callback_data="pay_donate_15")],
        [InlineKeyboardButton(text="⟡ 30 ⭐", callback_data="pay_donate_30")],
        [InlineKeyboardButton(text="⟡ 50 ⭐", callback_data="pay_donate_50")],
        [InlineKeyboardButton(text="← В меню", callback_data="back_menu")],
    ])
CMD_FEATURES: dict[str, dict] = {
    "ai": {
        "title": "◇ .ai",
        "desc": "Задай вопрос — ИИ ответит. Встроенный поиск: курс валют и крипты, погода, новости, веб. Может присылать готовые файлы: код, скрипты, сайты.",
        "usage": ".ai твой вопрос · .ai сделай файл",
        "example": ".ai курс доллара · .ai погода в Москве · .ai сделай калькулятор на python",
        "note": "Безлимитно. Работает в группах, каналах, бизнес-чатах. Модель GPT-OSS 120B"
    },
    "spam": {
        "title": "◇ .spam",
        "desc": "Отправляет N одинаковых сообщений в чат.",
        "usage": ".spam текст число",
        "example": ".spam Привет 10",
        "note": ".spam stop — остановить"
    },
    "mute": {
        "title": "◇ .mute",
        "desc": "Удаляет все сообщения от собеседника в личке.",
        "usage": ".mute — включить",
        "example": ".unmute — выключить",
        "note": "Только для личных чатов (Business)"
    },
    "nomute": {
        "title": "🛡 .nomute",
        "desc": "Дублирует твои сообщения от имени бота — если собеседник удаляет их, копия останется.",
        "usage": ".nomute — включить",
        "example": ".unnomute — выключить",
        "note": "Только для личных чатов (Business)"
    },
    "afk": {
        "title": "◇ .afk",
        "desc": "Автоответчик: «я не в сети».",
        "usage": ".afk [заметка]",
        "example": ".afk вернусь через час",
        "note": "Работает в ЛС с ботом и бизнес-чатах · .unafk — выключить"
    },
    "code": {
        "title": "◇ .code",
        "desc": "Всё, что пишешь — форматируется как код.",
        "usage": ".code — включить",
        "example": ".uncode — выключить",
        "note": "Текст в <pre><code>...</code></pre>"
    },
    "wbl": {
        "title": "◇ .wbl",
        "desc": "Удаляет мат и флуд от собеседника.",
        "usage": ".wbl — включить",
        "example": ".unwbl — выключить",
        "note": "Защита от мата, обфускации, флуда"
    },
    "price": {
        "title": "◇ .price",
        "desc": "Оценка стоимости юзернейма (как на Fragment).",
        "usage": ".price @username",
        "example": ".price @alimtona",
        "note": "В бизнес-чате без аргумента — оценит собеседника"
    },
    "info": {
        "title": "ℹ️ .info",
        "desc": "Карточка собеседника: имя, username, ID, bio, Premium, настройки приватности, фото профиля и примерная дата регистрации в Telegram.",
        "usage": ".info · .info @username · .info id",
        "example": ".info · .info @friend",
        "note": "В ЛС с ботом: .info @username — пришлёт и фото профиля · в бизнес-чате без аргумента — собеседник · в группе — ответь на сообщение или укажи @username"
    },
    "curs": {
        "title": "💱 .curs",
        "desc": "Курс популярных валют к рублю — официальные курсы ЦБ РФ, одним сообщением.",
        "usage": ".curs",
        "example": ".curs",
        "note": "Точный источник: ЦБ РФ · работает в ЛС (Business), группах, каналах и в ЛС с ботом"
    },
    "knb": {
        "title": "⚔️ .knb",
        "desc": "Камень-ножницы-бумага: 1×1 в ЛС и в группах по вызову.",
        "usage": ".knb — ЛС · .knb @user — группа",
        "example": ".knb @friend",
        "note": "Секретные ходы · случайный первый ход · счёт на реваншах"
    },
    "ramka": {
        "title": "🖼 .ramka",
        "desc": "Золотая орнаментальная рамка на фото — ответь на фото и напиши .ramka, и рамка наденется.",
        "usage": ".ramka (ответь на фото)",
        "example": "ответь на фото → .ramka",
        "note": "В ЛС с ботом: .ramka → пришли фото. В чатах: ответь на фото + .ramka"
    },
    "stik": {
        "title": "🏷 .stik",
        "desc": "Стикер из фото — ответь на фото и напиши .stik, и фото станет стикером.",
        "usage": ".stik (ответь на фото)",
        "example": "ответь на фото → .stik",
        "note": "В ЛС с ботом: .stik → пришли фото. В чатах: ответь на фото + .stik"
    },
    "krom": {
        "title": "🎥 .krom",
        "desc": "Кружок из видео — ответь на видео и напиши .krom, и видео станет кружком (квадрат 640×640, до 60 сек).",
        "usage": ".krom (ответь на видео)",
        "example": "ответь на видео → .krom",
        "note": "В ЛС с ботом: .krom → пришли видео. В чатах: ответь на видео + .krom"
    },
    "status": {
        "title": "📊 .status",
        "desc": "Статистика переписки с пользователем: сколько сообщений, кто больше писал, разбивка по медиа, объём текста, первое и последнее сообщение, пойманные удаления.",
        "usage": ".status · .status @username · .status id",
        "example": ".status · .status @friend",
        "note": "В бизнес-чате без аргумента — текущий собеседник · в группе — ответь на сообщение или укажи @username"
    },
    "bold": {
        "title": "◆ .bold",
        "desc": "Жирный шрифт — выдели текст жирным.",
        "usage": ".bold текст",
        "example": ".bold важное сообщение",
        "note": "Работает в группах и личных чатах (Business)"
    },
    "italic": {
        "title": "◆ .italic",
        "desc": "Курсив — наклонный текст.",
        "usage": ".italic текст",
        "example": ".italic нежный акцент",
        "note": "Работает в группах и личных чатах (Business)"
    },
    "mono": {
        "title": "◆ .mono",
        "desc": "Моноширинный шрифт — как код.",
        "usage": ".mono текст",
        "example": ".mono print('hello')",
        "note": "Работает в группах и личных чатах (Business)"
    },
    "line": {
        "title": "◆ .line",
        "desc": "Подчёркнутый текст.",
        "usage": ".line текст",
        "example": ".line подчёркнуто",
        "note": "Работает в группах и личных чатах (Business)"
    },
    "crossed": {
        "title": "◆ .crossed",
        "desc": "Зачёркнутый текст.",
        "usage": ".crossed текст",
        "example": ".crossed старый вариант",
        "note": "Работает в группах и личных чатах (Business)"
    },
    "hidden": {
        "title": "◆ .hidden",
        "desc": "Скрытый текст — спойлер, виден по тапу.",
        "usage": ".hidden текст",
        "example": ".hidden ответ на загадку",
        "note": "Работает в группах и личных чатах (Business)"
    },
    "quote": {
        "title": "◆ .quote",
        "desc": "Цитата — текст в блоке цитирования.",
        "usage": ".quote текст",
        "example": ".quote знание — сила",
        "note": "Работает в группах и личных чатах (Business)"
    },
    "sled": {
        "title": "🛰 .sled",
        "desc": "Следи за изменениями профиля — имя, юзернейм, bio, аватарка. Макс 3 цели на пользователя.",
        "usage": ".sled @username · .sled (список) · .unsled @username · .infosled @username",
        "example": ".sled @durov",
        "note": "Только в ЛС. Опрос каждые 5 мин. Онлайн/время в сети НЕ отслеживаются (ограничение Bot API)"
    },
}

def kb_cmd() -> InlineKeyboardMarkup:
    cmd_keys = ["ai", "spam", "mute", "nomute", "afk", "code", "wbl", "price", "info", "curs", "knb", "ramka", "stik", "krom", "status",
                "bold", "italic", "mono", "line", "crossed", "hidden", "quote", "sled"]
    rows = []
    for i in range(0, len(cmd_keys), 2):
        pair = cmd_keys[i:i+2]
        row = [InlineKeyboardButton(text=CMD_FEATURES[k]["title"], callback_data=f"cmd_info_{k}") for k in pair]
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✕ Закрыть", callback_data="cmd_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◆ Пользователи",   callback_data="adm_users")],
        [InlineKeyboardButton(text="◆ Статистика",     callback_data="adm_stats")],
        [InlineKeyboardButton(text="✦ Предложения",    callback_data="adm_ideas")],
        [InlineKeyboardButton(text="▤ Сообщение всем", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="▤ По группам/каналам", callback_data="adm_broadcast_groups")],
        [InlineKeyboardButton(text="← В меню",         callback_data="back_menu")],
    ])
SYSTEM_PROMPT = (
    "Ты сдержанный, элегантный ИИ-консьерж внутри Telegram-бота Quiet Mod. "
    "Отвечай чётко, без лишней воды. Язык — язык пользователя. "
    "СТРОГО: весь ответ должен быть на ОДНОМ языке — языке вопроса пользователя. "
    "Никогда не вставляй слова или фразы на других языках (вьетнамский, китайский и т.п.), "
    "даже одно слово — это критическая ошибка.\n\n"
    "Будь дружелюбным и полезным, держи стиль лаконичного люкса.\n\n"
    "ВАЖНО — ФОРМАТИРОВАНИЕ:\n"
    "— НИКОГДА не используй Markdown: никаких **, *, ##, ###, $$, \\(...\\), \\[...\\], _, ` и прочих символов разметки.\n"
    "— Пиши обычным текстом. Для выделения используй ТОЛЬКО Telegram HTML-теги: <b>жирный</b>, <i>курсив</i>.\n"
    "— Математические формулы пиши в читаемом виде, например: sqrt(x^2 + 4) + sqrt(x^2 + 1) = 3 - 5x^2\n"
    "— Списки оформляй через дефис или цифру с точкой, без Markdown-маркеров.\n"
    "— Если тебе дали результаты поиска, а они противоречат твоим знаниям — ВСЕГДА доверяй поиску: твои знания могли устареть (например, человек мог недавно умереть, курс измениться, выйти новая версия). Не спорь с поиском и не отвечай по памяти, если есть свежие данные.\n"
    "— Никаких LaTeX, никаких $...$ или $$...$$.\n\n"
    "КОД — ОТДЕЛЬНОЕ ПРАВИЛО:\n"
    "— Если тебя просят написать код (любой фрагмент от одной строки), "
    "всегда оборачивай его целиком в <pre><code>твой код тут</code></pre> — "
    "это отдельный блок, Telegram сам даёт пользователю кнопку «скопировать».\n"
    "— Внутри <pre><code>...</code></pre> код пиши как есть, без экранирования "
    "и без Markdown-разметки (без ```).\n"
    "— Короткое имя переменной, команду или путь к файлу внутри обычного текста "
    "оформляй одиночным <code>тегом</code> — не <pre>.\n"
    "— Не смешивай <b> или <i> внутри <pre><code>...</code></pre> — блок кода "
    "должен быть только с <pre><code> и ничем больше.\n\n"
    "ФАЙЛЫ — ОТДЕЛЬНОЕ ПРАВИЛО:\n"
    "— Если пользователь просит создать файл, скрипт, программу или код «файлом» — "
    "оборачивай каждый файл в маркер: <FILE name=\"calculator.py\">код</FILE>.\n"
    "— Файлов может быть НЕСКОЛЬКО подряд (например, проект: main.py, settings.py, README.md) — "
    "каждый файл в своём маркере.\n"
    "— Внутри маркера пиши код как есть: без ``` и без HTML-тегов.\n"
    "— Вне маркеров кратко опиши, что делает файл(ы) и как его запустить.\n"
    "— Не используй маркеры, если пользователь не просил файл — просто отвечай как обычно."
)

MEGERA_TEXT = (
    "◆ <b>МЕГЕРА</b> — мифическое существо, обитающее в недрах школы Денисовка.\n\n"
    "По преданиям старожилов, это создание появилось ещё в эпоху динозавров "
    "и с тех пор терроризирует учеников своим взглядом, от которого стынет кровь. "
    "Говорят, что если произнести её имя трижды в темноте — она явится с "
    "классным журналом и поставит двойку прямо в душу.\n\n"
    "◇ <i>Ареал обитания:</i> школа, Денисовка\n"
    "◇ <i>Опасность:</i> максимальная\n"
    "◇ <i>Питается:</i> нервами учеников и несданными домашними заданиями\n"
    "◇ <i>Защита:</i> выученный урок и дневник без помарок\n\n"
    "Берегите себя. Она везде. 👁"
)
EASTER_EGGS: list[tuple[list[str], str]] = [
    (["мегера", "анифе айдеровна", "анифе", "айдеровна"], MEGERA_TEXT),
]

def _check_easter_egg(text: str) -> Optional[str]:
    t = text.lower().strip()
    for keywords, response in EASTER_EGGS:
        if any(kw in t for kw in keywords):
            return response
    return None
def _ddg_text_sync(query: str, max_results: int = 5) -> list:
    try:
        return DDGS().text(query, region="ru-ru", safesearch="moderate", max_results=max_results)
    except Exception as e:
        log.warning(f"ddgs.text error: {e}")
        return []
def _ddg_news_sync(query: str, max_results: int = 5) -> list:
    try:
        return DDGS().news(query, region="ru-ru", safesearch="moderate", max_results=max_results)
    except Exception as e:
        log.warning(f"ddgs.news error: {e}")
        return []
async def _ddg_search(query: str, max_results: int = 5) -> str:
    try:
        is_news = any(w in query.lower() for w in ("новост", "news", "событ"))
        if is_news:
            results = await asyncio.to_thread(_ddg_news_sync, query, max_results)
            lines = [
                f"{r.get('title', '')} ({(r.get('date') or '')[:10]}): {r.get('body', '')}".strip()
                for r in results if r.get("title") or r.get("body")
            ]
            if lines:
                return "\n\n".join(lines)
        results = await asyncio.to_thread(_ddg_text_sync, query, max_results)
        lines = [
            f"{r.get('title', '')}: {r.get('body', '')}".strip(": ")
            for r in results if r.get("title") or r.get("body")
        ]
        return "\n\n".join(lines)
    except Exception as e:
        log.warning(f"DDG search error: {e}")
        return ""
WEATHER_CODES: dict[int, str] = {
    0: "☀️ Ясно",
    1: "🌤 Преимущественно ясно",
    2: "⛅ Переменная облачность",
    3: "☁️ Облачно",
    45: "🌫 Туман",
    48: "🌫 Изморозь",
    51: "🌦 Лёгкая морось",
    53: "🌦 Морось",
    55: "🌧 Сильная морось",
    56: "🌧 Ледяная морось",
    57: "🌧 Сильная ледяная морось",
    61: "🌧 Небольшой дождь",
    63: "🌧 Дождь",
    65: "🌧 Сильный дождь",
    66: "🌧 Ледяной дождь",
    67: "🌧 Сильный ледяной дождь",
    71: "🌨 Небольшой снег",
    73: "🌨 Снег",
    75: "❄️ Сильный снегопад",
    77: "❄️ Снежные зёрна",
    80: "🌧 Небольшие ливни",
    81: "🌧 Ливни",
    82: "⛈ Сильные ливни",
    85: "🌨 Небольшой снегопад",
    86: "❄️ Сильный снегопад",
    95: "⛈ Гроза",
    96: "⛈ Гроза с градом",
    99: "⛈ Сильная гроза с градом",
}
WEATHER_TRIGGERS = ("погод", "weather", "температур")

def _is_weather_query(text: str) -> bool:
    return any(k in text.lower() for k in WEATHER_TRIGGERS)
def _extract_city(text: str) -> str:
    t = f" {text.strip().lower()} "
    for kw in ("погодка", "погода", "погоду", "погоде", "weather",
               "температура", "температуру", "температуре"):
        t = t.replace(kw, " ")
    for w in (" в ", " на ", " по ", " какая ", " какой ", " сегодня ", " сейчас ", " завтра ",
              " прямо ", " там ", " in ", " at ", " is ", " the ", " today ", " now ", "?"):
        t = t.replace(w, " ")
    return t.strip(" ?!.,")
async def _geocode_city(session: aiohttp.ClientSession, city: str) -> Optional[dict]:
    candidates = [city]
    if len(city) > 4:
        candidates += [city[:-1], city[:-1] + "а", city[:-2]]
    for cand in candidates:
        try:
            async with session.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": cand, "count": 1, "language": "ru", "format": "json"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
                results = data.get("results")
                if results:
                    return results[0]
        except Exception:
            continue
    return None
async def _get_weather(city: str) -> Optional[str]:
    if not city:
        return None
    try:
        session = get_http()
        loc = await _geocode_city(session, city)
        if not loc:
            return None
        async with session.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": loc["latitude"],
                "longitude": loc["longitude"],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "forecast_days": 2,
                "timezone": "auto",
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            wx = await resp.json()
        cur = wx.get("current")
        if not cur:
            return None
        temp  = round(cur["temperature_2m"])
        feels = round(cur["apparent_temperature"])
        hum   = round(cur["relative_humidity_2m"])
        wind  = round(cur["wind_speed_10m"])
        desc  = WEATHER_CODES.get(int(cur.get("weather_code", 0)), "🌡 Погода")
        result = (
            f"{desc}\n"
            f"🌡 Температура: {temp:+d}°C, ощущается как {feels:+d}°C\n"
            f"💧 Влажность: {hum}%\n"
            f"💨 Ветер: {wind} км/ч"
        )
        daily = wx.get("daily")
        if daily and len(daily.get("time", [])) > 1:
            try:
                t_max = round(daily["temperature_2m_max"][1])
                t_min = round(daily["temperature_2m_min"][1])
                t_desc = WEATHER_CODES.get(int(daily["weather_code"][1]), "🌡")
                result += f"\n\nЗавтра: {t_desc}  {t_min:+d}°..{t_max:+d}°C"
            except (KeyError, IndexError, TypeError):
                pass
        return result
    except Exception as e:
        log.warning(f"Open-Meteo weather error: {e}")
        return None

# ─── Точный курс валют и крипты (встроен в .ai) ────────────────────────
_CURRENCY_CODES: dict[str, str] = {}   # алиас (нижний регистр) -> код валюты
_CURRENCY_NAMES: dict[str, str] = {}   # код -> человеческое название

def _register_cur(code: str, name: str, *aliases: str) -> None:
    _CURRENCY_NAMES[code] = name
    for a in aliases:
        _CURRENCY_CODES[a] = code

_register_cur("BTC", "биткоин", "биткоин", "bitcoin", "btc")
_register_cur("ETH", "эфир", "эфир", "эфириум", "ethereum", "eth")
_register_cur("USD", "доллар США", "доллар", "долар", "usd")
_register_cur("EUR", "евро", "евро", "eur")
_register_cur("RUB", "рубль", "рубл", "rub", "₽")
_register_cur("GBP", "фунт стерлингов", "фунт", "gbp")
_register_cur("CNY", "юань", "юан", "cny")
_register_cur("JPY", "японская йена", "йен", "иен", "jpy")
_register_cur("KZT", "тенге", "тенге", "kzt")
_register_cur("UAH", "гривна", "гривн", "uah")
_register_cur("TRY", "турецкая лира", "лир", "try")
_register_cur("CHF", "швейцарский франк", "франк", "chf")

def _currency_codes_in(text: str) -> list[str]:
    """Коды валют в порядке их появления в тексте (для выбора base/target)."""
    t = text.lower()
    found = [(t.find(alias), alias) for alias in _CURRENCY_CODES if t.find(alias) != -1]
    found.sort()
    codes: list[str] = []
    for _, alias in found:
        code = _CURRENCY_CODES[alias]
        if code not in codes:
            codes.append(code)
    return codes

def _is_currency_query(text: str) -> bool:
    codes = _currency_codes_in(text)
    if not codes:
        return False
    return not (len(codes) == 1 and codes[0] == "RUB")

def _fmt_rate(value: float) -> str:
    if value >= 100:
        return f"{value:,.0f}".replace(",", " ")
    if value >= 1:
        return f"{value:.2f}"
    return f"{value:.4f}"

async def _get_crypto_rate(session: aiohttp.ClientSession, crypto: list[str]) -> Optional[str]:
    ids = {"BTC": "bitcoin", "ETH": "ethereum"}
    id_str = ",".join(ids[c] for c in crypto if c in ids)
    if not id_str:
        return None
    async with session.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": id_str, "vs_currencies": "usd,rub"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
    lines = []
    for c in crypto:
        item = (data.get(ids.get(c)) or {})
        usd = item.get("usd")
        rub = item.get("rub")
        if usd is None:
            continue
        name = _CURRENCY_NAMES.get(c, c)
        line = f"🪙 <b>{name}</b>: <b>${_fmt_rate(float(usd))}</b>"
        if rub is not None:
            line += f" · ₽{_fmt_rate(float(rub))}"
        lines.append(line)
    if not lines:
        return None
    return "\n".join(lines) + "\n\n◐ <i>актуальный курс крипты</i>"

async def _get_currency_rate(query: str) -> Optional[str]:
    codes = _currency_codes_in(query)
    if not codes or (len(codes) == 1 and codes[0] == "RUB"):
        return None
    try:
        session = get_http()
        crypto = [c for c in codes if c in ("BTC", "ETH")]
        fiat = [c for c in codes if c not in ("BTC", "ETH")]
        if crypto:
            return await _get_crypto_rate(session, crypto)
        if not fiat:
            return None
        base = next((c for c in fiat if c != "RUB"), fiat[0])
        target = next((c for c in fiat if c != base), "RUB")
        if base == target:
            return None
        # Рублёвые пары — берём из общего кэша (Мосбиржа/ЦБ РФ), чтобы курс
        # в .ai совпадал с .curs и был «реальным», а не офшорным из er-api
        if base == "RUB" or target == "RUB":
            _, ru_rates = await _get_curs_cached()
            if ru_rates and ru_rates.get(target if base == "RUB" else base):
                try:
                    rate = ru_rates[base] if target == "RUB" else 1.0 / ru_rates[target]
                except ZeroDivisionError:
                    rate = None
                if rate:
                    base_name = _CURRENCY_NAMES.get(base, base)
                    target_name = _CURRENCY_NAMES.get(target, target)
                    return (
                        f"💱 <b>{base_name} → {target_name}</b>\n"
                        f"1 {base} = <b>{_fmt_rate(float(rate))} {target}</b>\n\n"
                        f"◐ <i>актуальный курс</i>"
                    )
        async with session.get(
            f"https://open.er-api.com/v6/latest/{base}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        rate = (data.get("rates") or {}).get(target)
        if rate is None:
            return None
        base_name = _CURRENCY_NAMES.get(base, base)
        target_name = _CURRENCY_NAMES.get(target, target)
        return (
            f"💱 <b>{base_name} → {target_name}</b>\n"
            f"1 {base} = <b>{_fmt_rate(float(rate))} {target}</b>\n\n"
            f"◐ <i>актуальный курс</i>"
        )
    except Exception as e:
        log.warning(f"Currency API error: {e}")
        return None

# ─── .curs — курс популярных валют к рублю ───────────────────────────
_POPULAR_CURS: list[tuple[str, str, str]] = [
    ("USD", "🇺🇸", "Доллар"),
    ("EUR", "🇪🇺", "Евро"),
    ("GBP", "🇬🇧", "Фунт"),
    ("CNY", "🇨🇳", "Юань"),
    ("JPY", "🇯🇵", "Йена"),
    ("CHF", "🇨🇭", "Франк"),
    ("KZT", "🇰🇿", "Тенге"),
    ("UAH", "🇺🇦", "Гривна"),
    ("TRY", "🇹🇷", "Лира"),
    ("BYN", "🇧🇾", "Бел. рубль"),
    ("AED", "🇦🇪", "Дирхам"),
    ("AMD", "🇦🇲", "Драм"),
    ("AZN", "🇦🇿", "Манат"),
    ("GEL", "🇬🇪", "Лари"),
    ("PLN", "🇵🇱", "Злотый"),
]

def _fmt_curs_rate(value: float) -> str:
    """Формат курса для .curs: 92,34 · 1 235 · 0,1923 (запятая как разделитель)"""
    return _fmt_rate(value).replace(".", ",")

# Живые пары Московской биржи (реальное время). Значение — ₽ за 1 ед.
# Проверяем каждый курс сверкой с ЦБ (разумный диапазон), чтобы отсечь
# контракты с другим номиналом (KZT/JPY — за 100) и неликвидный мусор.
_MOEX_CANDIDATES: dict[str, list[str]] = {
    "USD": ["USD000UTSTOM", "USDRUB_TOM", "USD000TODTOM"],
    "EUR": ["EURRUB_TOM", "EUR_RUB__TOM", "EUR000TODTOM"],
    "GBP": ["GBPRUB_TOM", "GBPRUB_TOD"],
    "CNY": ["CNYRUB_TOM", "CNY000000TOD"],
    "CHF": ["CHFRUB_TOM", "CHFRUB_TOD"],
    "TRY": ["TRYRUB_TOM", "TRYRUB_TOD"],
    "BYN": ["BYNRUB_TOM", "BYNRUB_TOD"],
}


async def _fetch_moex_rates() -> dict[str, float]:
    """Реальное время с Московской биржи: {код: ₽ за 1 ед.} для ликвидных пар."""
    try:
        session = get_http()
        url = "https://iss.moex.com/iss/engines/currency/markets/selt/boards/CETS/securities.json"
        params = {
            "iss.meta": "off",
            "iss.only": "marketdata",
            "marketdata.columns": "SECID,LAST",
            "limit": 500,
        }
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                log.warning(f"MOEX ISS status: {resp.status}")
                return {}
            data = await resp.json()
        md = data.get("marketdata") or {}
        cols = md.get("columns") or []
        rows = md.get("data") or []
        if "SECID" not in cols or "LAST" not in cols:
            return {}
        i_sec = cols.index("SECID")
        i_last = cols.index("LAST")
        last_by_sec: dict[str, float] = {}
        for row in rows:
            sec = row[i_sec] if i_sec < len(row) else None
            val = row[i_last] if i_last < len(row) else None
            if sec and isinstance(val, (int, float)) and 0.01 <= val <= 1_000_000:
                last_by_sec[str(sec)] = float(val)
        out: dict[str, float] = {}
        for code, secs in _MOEX_CANDIDATES.items():
            for s in secs:
                v = last_by_sec.get(s)
                if v:
                    out[code] = v
                    break
        return out
    except Exception as e:
        log.warning(f"MOEX ISS error: {e}")
        return {}


async def _fetch_cbr_rates() -> Optional[tuple[str, dict[str, float]]]:
    """Официальные курсы ЦБ РФ (cbr.ru, официальный источник): (дата, курс за 1 ед.)."""
    try:
        session = get_http()
        async with session.get(
            "https://www.cbr.ru/scripts/XML_daily.asp",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                log.warning(f"CBR status: {resp.status}")
                return None
            xml_text = await resp.text()
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_text)
        date_str = root.get("Date") or date.today().strftime("%d.%m.%Y")
        needed = {c for c, _, _ in _POPULAR_CURS}
        rates: dict[str, float] = {}
        for val in root.iter("Valute"):
            code = (val.findtext("CharCode") or "").strip()
            if code not in needed:
                continue
            try:
                nominal = float((val.findtext("Nominal") or "1").replace(",", "."))
                value = float((val.findtext("Value") or "").replace(",", "."))
            except (TypeError, ValueError):
                continue
            if nominal <= 0 or value <= 0:
                continue
            rates[code] = value / nominal
        return (date_str, rates) if rates else None
    except Exception as e:
        log.warning(f"CBR API error: {e}")
        return None

async def _fetch_erapi_rates() -> Optional[dict[str, float]]:
    """Запасной источник курсов: open.er-api.com (база RUB) — курс за 1 ед."""
    try:
        session = get_http()
        async with session.get(
            "https://open.er-api.com/v6/latest/RUB",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                log.warning(f"Curs API status: {resp.status}")
                return None
            data = await resp.json()
        raw = data.get("rates") or {}
        if not raw:
            return None
        rates: dict[str, float] = {}
        for code, _, _ in _POPULAR_CURS:
            per_rub = raw.get(code)
            if per_rub is None:
                continue
            try:
                rates[code] = 1.0 / float(per_rub)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        return rates or None
    except Exception as e:
        log.warning(f"Curs API error: {e}")
        return None

_CURS_TTL_SECONDS = 30 * 60  # кэш курсов: обновление каждые 30 минут
_curs_cache: dict = {"ts": 0.0, "text": "", "rates": {}, "source": "", "date_str": ""}
_curs_refresh_lock = asyncio.Lock()  # защита от дублирующих запросов при холодном кэше


def _render_curs_text(rates: dict[str, float], source: str, date_str: str) -> Optional[str]:
    lines = []
    for code, flag, name in _POPULAR_CURS:
        rub = rates.get(code)
        if rub is None or rub <= 0:
            continue
        lines.append(f"{flag} {name:<12} → <code>{_fmt_curs_rate(rub):>10} ₽</code>")
    if not lines:
        return None
    return (
        "💱 <b>КУРС ВАЛЮТ К РУБЛЮ</b>\n"
        f"<code>{LINE}</code>\n\n"
        + "\n".join(lines)
        + f"\n\n<code>{LINE}</code>\n"
        f"◇ 1 ед. валюты = ₽ · ◐ {source} · {date_str}\n"
        f"— 👁️ @{BOT_USERNAME}"
    )


async def _refresh_curs_cache() -> bool:
    """Обновляет кэш курсов: Мосбиржа (реальное время) → официальный ЦБ → er-api только для недостающих.

    Основные пары берём с Мосбиржи, но только если значение правдоподобно
    (в разумном диапазоне от курса ЦБ) — иначе берём официальный курс ЦБ.
    """
    source = "актуальный курс"
    date_str = date.today().strftime("%d.%m.%Y")
    cbr = await _fetch_cbr_rates()
    moex = await _fetch_moex_rates()
    if cbr:
        date_str, rates = cbr
        rates = dict(rates)
        source = "официальный курс ЦБ РФ"
        used_moex = []
        for code, rub in moex.items():
            base = rates.get(code)
            if base and base > 0 and 0.9 <= rub / base <= 1.1:
                rates[code] = rub
                used_moex.append(code)
        if used_moex:
            source = "Мосбиржа · реальный рынок"
            if len(used_moex) < len(_POPULAR_CURS):
                source = "Мосбиржа + ЦБ РФ"
    else:
        # ЦБ недоступен — берём Мосбиржу для основных пар (все они за 1 ед.)
        rates = dict(moex)
        source = "Мосбиржа · реальный рынок"
    missing = [c for c, _, _ in _POPULAR_CURS if c not in rates]
    if missing:
        er = await _fetch_erapi_rates()
        if er:
            for c in missing:
                if c in er:
                    rates[c] = er[c]
            source += " · часть валют — er-api"
    if not rates:
        return False
    text = _render_curs_text(rates, source, date_str)
    if not text:
        return False
    _curs_cache.update({
        "ts": asyncio.get_running_loop().time(),
        "text": text,
        "rates": rates,
        "source": source,
        "date_str": date_str,
    })
    return True


async def _get_curs_cached() -> tuple[Optional[str], Optional[dict[str, float]]]:
    """Курс валют к рублю с кэшем 30 минут: (текст, {код: ₽ за 1 ед.}).

    Если API недоступны — отдаём последний известный курс (ответ не теряется).
    """
    now = asyncio.get_running_loop().time()
    cache = _curs_cache
    if cache.get("text") and now - cache.get("ts", 0) < _CURS_TTL_SECONDS:
        return cache["text"], cache.get("rates") or None
    async with _curs_refresh_lock:
        now = asyncio.get_running_loop().time()
        if cache.get("text") and now - cache.get("ts", 0) < _CURS_TTL_SECONDS:
            return cache["text"], cache.get("rates") or None
        if await _refresh_curs_cache():
            return cache["text"], cache.get("rates")
    if cache.get("text"):
        return (
            cache["text"] + "\n\n◇ <i>данные могли устареть — сервис курсов временно недоступен</i>",
            cache.get("rates") or None,
        )
    return None, None

SEARCH_TRIGGERS = [
    "курс", "цена", "цены", "стоимость", "сколько стоит", "подорожал",
    "погода", "новости", "новость", "событ",
    "сегодня", "сейчас", "текущ", "актуальн", "последние", "последняя",
    "вышел", "вышла", "вышло", "выйдет", "релиз", "анонс", "анонсировал",
    "обновление", "обновили", "версия",
    "кто выиграл", "результат", "счёт", "матч", "турнир", "чемпионат",
    "кто такой", "кто такая", "кто сейчас", "что за", "что такое",
    "случилось", "произошло", "заявил", "объявил",
    "жив ли", "умер", "скончался", "существует ли",
]

def _needs_search(reply: str, user_msg: str) -> bool:
    reply_lower = reply.lower()
    uncertainty_phrases = [
        "не знаю", "не могу знать", "нет информации", "нет данных",
        "актуальн", "последн", "свежи", "сейчас", "на данный момент",
        "у меня нет доступа", "моя информация", "обрати́сь к",
        "рекомендую проверить", "уточни", "не уверен",
        "cannot", "don't know", "i don't have", "as of my",
        "my knowledge", "i'm not sure", "check online",
    ]
    if any(p in reply_lower for p in uncertainty_phrases):
        return True
    if any(t in user_msg.lower() for t in SEARCH_TRIGGERS):
        return True
    current_year = date.today().year
    if re.search(r"\b(20[2-9]\d)\b", user_msg) and any(
        str(y) in user_msg for y in range(current_year - 2, current_year + 2)
    ):
        return True
    return False
def _needs_search_preemptive(user_msg: str) -> bool:
    t = user_msg.lower()
    if any(tr in t for tr in SEARCH_TRIGGERS):
        return True
    current_year = date.today().year
    if re.search(r"\b(20[2-9]\d)\b", user_msg) and any(
        str(y) in user_msg for y in range(current_year - 2, current_year + 2)
    ):
        return True
    return False

# ─── Запросы о смерти/здоровье: не даём боту «лгать» по устаревшим знаниям ──
_DEATH_RE = re.compile(
    r"(?iu)\b(жив ли|жива ли|живы ли|жив ли он|жива ли она|"
    r"умер\b|умерла\b|умерли\b|умерших\b|умершего\b|скончался\b|скончалась\b|скончались\b|"
    r"погиб\b|погибла\b|погибли\b|не стало\b|дата смерти\b|в живых\b|"
    r"мертв\b|мертва\b|убит\b|убита\b|убитый\b|смерть\b)"
)

_DEATH_STOPWORDS = {
    "когда", "где", "как", "почему", "кто", "что", "он", "она", "они",
    "его", "её", "их", "этот", "эта", "это", "такое", "такой", "такая",
    "ещё", "сейчас", "мне", "в", "на", "по", "ли", "был", "была", "были",
    "про", "о", "значит", "означает", "вообще", "даже",
}

def _is_death_query(text: str) -> bool:
    return bool(_DEATH_RE.search(text or ""))

def _extract_death_subject(text: str) -> str:
    """Вытаскивает имя/субъект из запроса о смерти («жив ли Иван» -> «иван»)."""
    t = (text or "").lower()
    _DEATH_KWS = (
        "жив ли ещё", "жив ли", "жива ли", "живы ли", "умер ли", "умерла ли",
        "когда умер", "когда умерла", "умерших", "умершего", "умерший",
        "умер", "умерла", "умерли", "скончался", "скончалась", "скончались",
        "погиб", "погибла", "погибли", "в живых", "дата смерти", "смерть",
        "не стало", "мертв", "мертва", "убит", "убита", "убитый",
        "что случилось с", "что стало с",
    )
    # длинные формы раньше коротких: «умерла» до «умер», «погибла» до «погиб» и т.д.
    for kw in sorted(_DEATH_KWS, key=len, reverse=True):
        t = t.replace(kw, " ")
    t = re.sub(r"[?!.,:;«»\"'()]+", " ", t)
    words = [
        w for w in t.split()
        if len(w) > 1 and w not in _DEATH_STOPWORDS and not w.startswith(("@", "http"))
    ]
    return " ".join(words).strip() or text

_ALIVE_RE = re.compile(
    r"(?iu)\b(он жив|она жива|они живы|жив и здоров|жива и здорова|"
    r"alive|живой|живая|живые|жива|жив)\b"
)

def _reply_claims_alive(text: str) -> bool:
    """Ответ ИИ утверждает, что человек жив (нужна перепроверка)."""
    return bool(_ALIVE_RE.search(text or ""))

def _sanitize_text_messages(messages: list) -> list:
    """Гарантирует формат для модели Groq: content всегда строка.

    Некорректные типы (списки, None и т.п.) приводятся к строке —
    это защита от битых сообщений в истории. Работаем с копиями —
    исходная история не изменяется.
    """
    out: list[dict] = []
    for m in messages:
        m = dict(m)
        content = m.get("content")
        if isinstance(content, str):
            out.append(m)
            continue
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    t = part.get("text")
                    if isinstance(t, str) and t.strip():
                        text_parts.append(t.strip())
            m["content"] = "\n".join(text_parts) if text_parts else "[изображение]"
        elif content is None:
            m["content"] = ""
        else:
            m["content"] = str(content)
        out.append(m)
    return out


async def _groq_request(messages: list, max_tokens: int = 2048, temperature: float = 0.7, model: str = GROQ_MODEL) -> Optional[str]:
    """Отправка запроса в Groq с фолбэком между API-ключами.

    Если ключ не отвечает (лимит токенов, ошибка, таймаут) — бот
    автоматически пробует следующий ключ из GROQ_API_KEYS.
    Каждый следующий запрос начинается с последнего рабочего ключа.
    Модель у всех ключей одна и та же (параметр model).

    Перед отправкой история приводится к формату модели: content всегда строка.
    """
    messages = _sanitize_text_messages(messages)
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if model == GROQ_MODEL:
        # GPT-OSS 120B — reasoning-модель: по умолчанию думает на «medium»,
        # это тысячи токенов на каждый запрос (которые потом всё равно
        # вырезаются как <think>). Ставим «low» — ответы те же, а трата
        # токенов (и риск TPM-лимита) в разы меньше.
        payload["reasoning_effort"] = "low"
    global _GROQ_KEY_INDEX
    keys = GROQ_API_KEYS
    n = len(keys)
    if n == 0:
        log.error("Groq: нет ни одного API-ключа (GROQ_API_KEY / GROQ_API_KEY2 / GROQ_API_KEY3)")
        return None
    start = _GROQ_KEY_INDEX % n
    rate_limited = False  # 413 (TPM-лимит) общий для всех ключей — ротация бессмысленна
    for i in range(n):
        idx = (start + i) % n
        api_key = keys[idx]
        if rate_limited:
            # Все ключи в одном тире с одним TPM-лимитом: вместо ротации ждём,
            # чтобы лимит (8000 токенов/мин) успел сброситься, и пробуем снова.
            await asyncio.sleep(5)
        try:
            session = get_http()
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as resp:
                import json as _json
                raw = await resp.text()
                try:
                    data = _json.loads(raw)
                except Exception:
                    log.error(
                        f"Groq non-JSON response (key={idx + 1}/{n}, model={model}, "
                        f"status={resp.status}, body={raw[:300]})",
                        exc_info=True,
                    )
                    continue
                if resp.status != 200:
                    err = data.get("error") if isinstance(data, dict) else data
                    log.warning(
                        f"Groq key {idx + 1}/{n} failed (model={model}, status={resp.status}): "
                        f"{_json.dumps(err, ensure_ascii=False)[:200]}"
                    )
                    await db.record_stat(f"groq_key{idx + 1}_fail", f"status={resp.status}")
                    if resp.status == 413:
                        rate_limited = True  # TPM-лимит — общий, ждём и пробуем тот же ключ
                    continue
                if "choices" not in data:
                    log.error(
                        f"Groq unexpected response (key={idx + 1}/{n}, model={model}, "
                        f"status={resp.status}, body={_json.dumps(data, ensure_ascii=False)[:300]})"
                    )
                    continue
                _GROQ_KEY_INDEX = idx
                await db.record_stat(f"groq_key{idx + 1}_ok", model)
                return data["choices"][0]["message"]["content"].strip()
        except asyncio.TimeoutError:
            log.warning(f"Groq request timeout (key={idx + 1}/{n}, model={model}) — пробую следующий ключ")
            await db.record_stat(f"groq_key{idx + 1}_fail", "timeout")
        except aiohttp.ClientError as e:
            log.error(f"Groq HTTP request failed (key={idx + 1}/{n}, model={model}): {e!r}")
            await db.record_stat(f"groq_key{idx + 1}_fail", "http_error")
        except Exception as e:
            log.error(f"Groq request failed (key={idx + 1}/{n}, model={model}): {e!r}")
            await db.record_stat(f"groq_key{idx + 1}_fail", "error")
    log.error(f"Groq: все {n} API-ключей не сработали (model={model})")
    return None
def _normalize_code_blocks(text: str) -> str:
    text = re.sub(
        r"```(?:\w+)?\n?(.*?)```",
        lambda m: f"<pre><code>{html_escape(m.group(1))}</code></pre>",
        text,
        flags=re.DOTALL,
    )
    parts = re.split(r"(<pre><code>.*?</code></pre>)", text, flags=re.DOTALL)
    for i, part in enumerate(parts):
        if part.startswith("<pre><code>"):
            continue
        parts[i] = re.sub(r"`([^`\n]+)`", lambda m: f"<code>{html_escape(m.group(1))}</code>", part)
    return "".join(parts)
def _looks_like_bad_html(description: Optional[str]) -> bool:
    if not description:
        return False
    return "can't parse entities" in description.lower()
async def _reply_ai_html(msg: Message, prefix: str, answer: str, reply_markup=None, use_reply: bool = False):
    text = f"{prefix}{answer}" if prefix else answer
    send = msg.reply if use_reply else msg.answer
    try:
        return await send(text, reply_markup=reply_markup)
    except Exception as e:
        if "can't parse entities" in str(e).lower() or "parse entities" in str(e).lower():
            log.warning(f"AI reply bad HTML, falling back to escaped: {e}")
            fallback = f"{prefix}{html_escape(answer)}" if prefix else html_escape(answer)
            return await send(fallback, reply_markup=reply_markup)
        raise
async def _edit_ai_html(target_msg: Message, prefix: str, answer: str):
    text = f"{prefix}{answer}" if prefix else answer
    try:
        await target_msg.edit_text(text)
    except Exception as e:
        if "can't parse entities" in str(e).lower() or "parse entities" in str(e).lower():
            log.warning(f"AI edit bad HTML, falling back to escaped: {e}")
            fallback = f"{prefix}{html_escape(answer)}" if prefix else html_escape(answer)
            await target_msg.edit_text(fallback)
        else:
            raise
async def _business_edit_ai_html(conn_id: str, chat_id: int, msg_id: int, prefix: str, answer: str) -> bool:
    text = f"{prefix}{answer}"
    ok, description = await _business_edit_message_ex(conn_id, chat_id, msg_id, text)
    if not ok and _looks_like_bad_html(description):
        log.warning(f"Business AI edit bad HTML, falling back to escaped: {description}")
        fallback = f"{prefix}{html_escape(answer)}"
        ok, _ = await _business_edit_message_ex(conn_id, chat_id, msg_id, fallback)
    return ok


# ─── Файлы от ИИ: <FILE name="...">код</FILE> → настоящие документы ─────
_FILE_RE = re.compile(
    r"<FILE\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</FILE>",
    re.IGNORECASE | re.DOTALL,
)

def _safe_filename(name: str) -> str:
    """Приводит имя файла к безопасному виду: без путей, без лишних символов, с расширением."""
    name = (name or "").strip().replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[^\w.\-]+", "_", name)
    if not name or name in (".", ".."):
        return "file.txt"
    if "." not in name:
        name += ".txt"
    if len(name) > 64:
        ext = name.rsplit(".", 1)[-1]
        name = name[:60] + "." + ext
    return name

_FILE_TRIGGERS = ("файл", "скрипт", "программ", "сделай код", "напиши код", "сгенерируй код", "мини-игр", "игру на")

def _wants_file(text: str) -> bool:
    """Пользователь просит создать файл/код файлом — даём модели больше токенов."""
    t = (text or "").lower()
    return any(k in t for k in _FILE_TRIGGERS)

def _parse_code_files(text: str) -> tuple[str, list[dict]]:
    """Извлекает <FILE name="...">код</FILE>-блоки из ответа ИИ.

    Возвращает (текст без маркеров, [{"name": ..., "content": ...}]).
    """
    text = text or ""
    files: list[dict] = []
    def _extract(match: re.Match) -> str:
        content = (match.group(2) or "").strip("\r\n \t")
        content = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", content).strip("\r\n \t")
        if content:
            files.append({"name": _safe_filename(match.group(1)), "content": content})
        return ""
    cleaned = _FILE_RE.sub(_extract, text)
    cleaned = re.sub(r"<FILE\b[^>]*>|</FILE>", "", cleaned, flags=re.IGNORECASE)
    return cleaned, files

async def _send_code_files(chat_id: int, files: list[dict], business_connection_id: Optional[str] = None) -> None:
    """Отправляет готовые файлы (код) как документы в чат."""
    for f in files:
        name = f.get("name") or "file.txt"
        content = (f.get("content") or "").strip()
        if not content:
            continue
        try:
            await bot.send_document(
                chat_id,
                document=BufferedInputFile(content.encode("utf-8"), filename=name),
                business_connection_id=business_connection_id,
            )
            log.info(f"📄 Файл отправлен: {name} → chat={chat_id}")
        except Exception as e:
            log.warning(f"send document {name}: {e}")

def _strip_think_blocks(text: str) -> str:
    """Вырезает reasoning-блоки <think>...</think> из ответа модели.

    GPT-OSS 120B (как и другие reasoning-модели) иногда вставляет в ответ
    «мыслительный» блок <think>...</think> — его нужно убирать, чтобы
    пользователь видел только чистый ответ. Блок может начинаться с «◆ ».
    """
    if not text:
        return text
    t = re.sub(r"(?:◆\s*)?<think>.*?</think>\s*", "", text, flags=re.S).strip()
    # Незакрытый <think>: модель может обрезаться на лимите токенов прямо
    # посреди «мышления». Мышление всегда идёт ДО ответа, поэтому всё,
    # что идёт после последнего незакрытого <think> — мусор, отрезаем.
    idx = t.lower().rfind("<think")
    if idx != -1 and "</think" not in t[idx:].lower():
        cut = t.rfind("\n", 0, idx)
        t = (t[:cut] if cut != -1 else t[:idx]).rstrip()
    return t.strip()


async def groq_chat(uid: int, user_msg: str) -> tuple[str, list[dict]]:
    egg = _check_easter_egg(user_msg)
    if egg:
        return egg, []
    history = ai_history.setdefault(uid, [])
    history.append({"role": "user", "content": user_msg})
    if len(history) > 10:
        ai_history[uid] = history[-10:]
        history = ai_history[uid]
    active_model = GROQ_MODEL
    already_searched = False
    if _is_weather_query(user_msg):
        city = _extract_city(user_msg)
        weather_text = await _get_weather(city) if city else None
        if weather_text:
            reply = weather_text + "\n\n◐ <i>точные данные о погоде</i>"
            ai_history[uid].append({"role": "assistant", "content": reply})
            return reply, []
        if city:
            reply = f"⚠️ Не нашёл город «{city}» — уточни название и спроси ещё раз."
            ai_history[uid].append({"role": "assistant", "content": reply})
            return reply, []
    if _is_currency_query(user_msg):
        currency_text = await _get_currency_rate(user_msg)
        if currency_text:
            log.info(f"💱 Currency rate for uid={uid}: {user_msg[:60]}")
            ai_history[uid].append({"role": "assistant", "content": currency_text})
            return currency_text, []
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    today_str = date.today().strftime("%d.%m.%Y")
    is_death = _is_death_query(user_msg)
    search_query = user_msg
    if is_death or _needs_search_preemptive(user_msg):
        log.info(f"🔍 Preemptive search for uid={uid}: {user_msg[:60]}")
        if is_death:
            subject = _extract_death_subject(user_msg)
            search_query = f"{subject} умер" if subject and subject != user_msg else user_msg
        search_results = await _ddg_search(search_query)
        if search_results:
            if is_death:
                search_instr = (
                    f"[Результаты поиска по запросу «{search_query}»]\n\n"
                    f"{search_results}\n\n"
                    f"Сегодня: {today_str}. Пользователь спрашивает про человека, который "
                    "мог недавно умереть. Данные поиска НОВЕЕ твоих знаний: если в "
                    "результатах есть информация о смерти (дата, обстоятельства) — "
                    "ТЫ ОБЯЗАН сообщить её, даже если это противоречит тому, что ты "
                    "знал раньше. Ответь чётко: жив или умер (с датой), на языке "
                    "пользователя, кратко."
                )
            else:
                search_instr = (
                    f"[Результаты поиска по запросу «{user_msg}»]\n\n"
                    f"{search_results}\n\n"
                    f"Сегодня: {today_str}. Используй эти данные, если они релевантны "
                    "вопросу — дай актуальный и точный ответ. Отвечай на языке "
                    "пользователя, кратко и по делу."
                )
            messages = messages + [{"role": "user", "content": search_instr}]
            already_searched = True
    max_tokens = 8192 if _wants_file(user_msg) else 2048
    reply = await _groq_request(messages, max_tokens=max_tokens, model=active_model)
    reply = _strip_think_blocks(reply)
    if reply is None:
        return (
            "◆ <b>ИИ недоступен</b> — вероятно, исчерпан бесплатный лимит токенов на сегодня.\n\n"
            "◇ Подожди немного и попробуй ещё раз.\n\n"
            "Quiet Mod — бесплатный бот для всех.\n"
            "Спасибо за терпение и уважение ◆",
            [],
        )
    reply, files = _parse_code_files(reply)
    reply = _normalize_code_blocks(reply)
    if already_searched:
        reply += "\n\n◐ <i>ответ дополнен поиском</i>"
    if not already_searched and _needs_search(reply, user_msg):
        log.info(f"🔍 Fallback search triggered for uid={uid}: {user_msg[:60]}")
        search_results = await _ddg_search(search_query)
        if search_results:
            augmented_messages = messages + [
                {"role": "assistant", "content": reply},
                {
                    "role": "user",
                    "content": (
                        f"[Результаты поиска по запросу «{search_query}»]\n\n"
                        f"{search_results}\n\n"
                        "На основе этих данных дай актуальный и точный ответ. "
                        "Если информация из поиска полезна — используй её. "
                        "Отвечай на языке пользователя, кратко и по делу."
                    )
                }
            ]
            reply_with_search = await _groq_request(augmented_messages, max_tokens=max_tokens, model=active_model)
            if reply_with_search:
                reply_with_search = _strip_think_blocks(reply_with_search)
                reply_with_search, extra_files = _parse_code_files(reply_with_search)
                files += extra_files
                reply = _normalize_code_blocks(reply_with_search) + "\n\n◐ <i>ответ дополнен поиском</i>"
                log.info(f"🔍 Search augmented reply for uid={uid}")
    if is_death and _reply_claims_alive(reply):
        log.info(f"⚰️ Death-check verify uid={uid}: {user_msg[:60]}")
        subject = _extract_death_subject(user_msg)
        vq = f"{subject} умер" if subject and subject != user_msg else f"{user_msg} дата смерти"
        v_results = await _ddg_search(vq)
        if v_results:
            v_messages = messages + [
                {"role": "assistant", "content": reply},
                {
                    "role": "user",
                    "content": (
                        f"[Новые результаты поиска: «{vq}»]\n\n"
                        f"{v_results}\n\n"
                        f"Сегодня: {today_str}. Важно: ранее ты ответил, что человек жив. "
                        "Перепроверь по этим данным. Если они сообщают о смерти — "
                        "исправь ответ и укажи дату. Данные поиска НОВЕЕ твоих знаний: "
                        "доверяй им. Ответь кратко, на языке пользователя."
                    )
                },
            ]
            v_reply = await _groq_request(v_messages, max_tokens=max_tokens, model=active_model)
            if v_reply:
                v_reply = _strip_think_blocks(v_reply)
                v_reply, extra_files = _parse_code_files(v_reply)
                files += extra_files
                reply = _normalize_code_blocks(v_reply) + "\n\n◐ <i>перепроверено по свежим данным</i>"
                log.info(f"⚰️ Death-check corrected uid={uid}")
    ai_history[uid].append({"role": "assistant", "content": reply})
    return reply, files
