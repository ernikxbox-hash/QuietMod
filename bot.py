"""
╔══════════════════════════════════════════════════════╗
║              QUIET MOD 👁️  —  Black Luxury           ║
║   Telegram Business · SQLite · Groq · Stars · Rly   ║
╚══════════════════════════════════════════════════════╝
"""
import asyncio
import logging
import os
import re
import signal
from datetime import date, timedelta, timezone
from typing import Optional

import aiohttp
from ddgs import DDGS
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BusinessMessagesDeleted,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from html import escape as html_escape
import database as db

# ══════════════════════════════════════════════════════
#  CONFIG  (Railway: задай переменные окружения)
# ══════════════════════════════════════════════════════
BOT_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID     = int(os.environ["ADMIN_ID"])
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
BOT_USERNAME = "Quiet_Mod_bot"  # фиксированное имя — не зависит от старой переменной окружения
GROQ_MODEL       = "meta-llama/llama-4-scout-17b-16e-instruct"  # мультимодальная — нужна для фото
GROQ_MODEL_TEXT  = "llama-3.3-70b-versatile"  # для текста — стабильнее, без языковых глюков Scout

# Название бренда (используется в текстах)
BRAND_NAME = "Quiet Mod 👁️"

# Сколько звёзд за что
PREMIUM_MONTHLY_STARS = 50
DONOR_BADGE_MIN       = 100

# ══════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

# ══════════════════════════════════════════════════════
#  BOT & DISPATCHER
# ══════════════════════════════════════════════════════
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher(storage=MemoryStorage())

# in-memory кэш истории ИИ
ai_history: dict[int, list] = {}

# Последнее уведомление (deleted/edited) на owner_id
# owner_id → message_id уведомления, которое нужно удалить при следующем уведомлении
last_notify_msg: dict[int, int] = {}

# Главное меню-сообщение бота для каждого пользователя (редактируется вместо отправки нового)
# uid → message_id главного сообщения
home_msg: dict[int, int] = {}


# ══════════════════════════════════════════════════════
#  FSM
# ══════════════════════════════════════════════════════
class S(StatesGroup):
    ai_chat      = State()
    ai_search    = State()
    suggest_idea = State()
    broadcast    = State()


# ══════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════
LINE = "──────────────────"

# Telegram отдаёт время сообщений в UTC — переводим в МСК (UTC+3, без перевода стрелок)
MSK = timezone(timedelta(hours=3))


def fmt_msg_date(dt) -> str:
    """Форматирует datetime сообщения в МСК (dd.mm.YYYY · HH:MM)."""
    return dt.astimezone(MSK).strftime("%d.%m.%Y · %H:%M")


def ref_link(uid: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"


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


def premium_badge(is_prem: bool, donor: bool) -> str:
    if donor:  return "◇"
    if is_prem: return "◈"
    return ""


def fmt_sender(from_name: str, username: str) -> str:
    """Красиво форматирует имя + username отправителя."""
    if username:
        return f"{from_name} ({username})"
    return from_name


def home_text(is_prem: bool) -> str:
    """
    Единая карточка главного экрана — используется в /start и во всех
    callback'ах, возвращающих в главное меню (back_menu, ack_, del_).
    Один источник правды для дизайна, чтобы все экраны были идентичны.
    """
    status = "VIP-статус" if is_prem else "Базовый доступ"
    return (
        f"◆ <b>QUIET MOD</b> 👁️\n"
        f"<code>{LINE}</code>\n\n"
        f"◇ Статус       <b>{status}</b>\n"
        f"◇ Перехват     <b>безлимит</b>\n"
        f"◇ Архив        <b>{'200' if is_prem else '20'} записей</b>\n"
        f"◇ ИИ           <b>без лимитов</b>\n"
        f"<code>{LINE}</code>"
    )


async def _show_home(uid: int, text: str, reply_markup, target_msg: "Message | None" = None):
    """
    Показывает главное меню, редактируя уже существующее сообщение если возможно,
    иначе отправляет новое и запоминает его id.
    target_msg — сообщение пользователя (для отправки нового если нет home).
    """
    existing_id = home_msg.get(uid)
    if existing_id and target_msg:
        try:
            await bot.edit_message_text(
                text, chat_id=uid, message_id=existing_id,
                reply_markup=reply_markup, parse_mode="HTML"
            )
            return
        except Exception:
            pass  # сообщение удалено — отправим новое
    if target_msg:
        sent = await target_msg.answer(text, reply_markup=reply_markup)
    else:
        sent = await bot.send_message(uid, text, reply_markup=reply_markup)
    home_msg[uid] = sent.message_id


async def _send_notify(owner_id: int, text: str, reply_markup=None) -> Optional[int]:
    """
    Отправляет уведомление об удалённом/изменённом сообщении,
    предварительно удаляя предыдущее уведомление (чтобы не засорять чат).
    Возвращает message_id нового уведомления.
    """
    # Удаляем старое уведомление если есть
    old_id = last_notify_msg.get(owner_id)
    if old_id:
        try:
            await bot.delete_message(owner_id, old_id)
        except Exception:
            pass  # уже удалено или недоступно
        last_notify_msg.pop(owner_id, None)

    try:
        sent = await bot.send_message(owner_id, text, reply_markup=reply_markup)
        last_notify_msg[owner_id] = sent.message_id
        return sent.message_id
    except Exception as e:
        log.error(f"send notify to owner={owner_id}: {e}")
        return None


# ══════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════
def kb_main(uid: int, is_prem: bool) -> InlineKeyboardMarkup:
    rows = []
    if uid == ADMIN_ID:
        rows.append([InlineKeyboardButton(text="▲ Admin Suite", callback_data="adm")])
    # Блок перехватов
    rows.append([
        InlineKeyboardButton(text="▣ Архив",        callback_data="show_all"),
        InlineKeyboardButton(text="◆ Профиль",      callback_data="stats"),
    ])
    rows.append([
        InlineKeyboardButton(text="◈ Сохранённые ➩", callback_data="show_saved"),
    ])
    # Поиск только для premium
    if is_prem:
        rows.append([InlineKeyboardButton(text="◐ Поиск по архиву", callback_data="search")])
    # Блок ИИ
    rows.append([InlineKeyboardButton(text="◆ ИИ-консьерж — без лимитов", callback_data="ai_open")])
    # Блок прочего
    rows.append([
        InlineKeyboardButton(text="⟡ Приглашения", callback_data="referrals"),
        InlineKeyboardButton(text="✕ Очистить",    callback_data="clear_cache"),
    ])
    rows.append([
        InlineKeyboardButton(text="✦ Предложить",   callback_data="suggest_idea"),
        InlineKeyboardButton(text="⚙ Подключение",  callback_data="howto"),
    ])
    # Premium CTA
    if is_prem:
        rows.append([InlineKeyboardButton(text="◈ VIP-статус активен — продлить", callback_data="premium_info")])
    else:
        rows.append([InlineKeyboardButton(text="◈ VIP · 50★/мес — расширить архив", callback_data="premium_info")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back(target: str = "menu", label: str = "← В меню") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"back_{target}")]
    ])


def kb_notify(save_id: int) -> InlineKeyboardMarkup:
    """Клавиатура под уведомлением об удалённом/изменённом сообщении."""
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


def kb_premium() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◈ VIP · 50★ — 1 месяц",        callback_data="pay_premium_50")],
        [InlineKeyboardButton(text="─────────────────",  callback_data="noop")],
        [InlineKeyboardButton(text="◇ Вклад · 100★  (+30 дн. VIP)", callback_data="pay_donate_100")],
        [InlineKeyboardButton(text="◇ Вклад · 200★  (+30 дн. VIP)", callback_data="pay_donate_200")],
        [InlineKeyboardButton(text="◇ Вклад · 500★  (+30 дн. VIP)", callback_data="pay_donate_500")],
        [InlineKeyboardButton(text="← В меню",                       callback_data="back_menu")],
    ])


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◆ Пользователи",   callback_data="adm_users")],
        [InlineKeyboardButton(text="◆ Статистика",     callback_data="adm_stats")],
        [InlineKeyboardButton(text="✦ Предложения",    callback_data="adm_ideas")],
        [InlineKeyboardButton(text="▤ Сообщение всем", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="← В меню",         callback_data="back_menu")],
    ])


# ══════════════════════════════════════════════════════
#  GROQ AI  (без лимитов — Groq бесплатный)
# ══════════════════════════════════════════════════════
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
    "должен быть только с <pre><code> и ничем больше."
)


async def _get_image_base64(bot: Bot, file_id: str) -> Optional[str]:
    """Скачивает фото из Telegram и возвращает base64 строку."""
    try:
        file = await bot.get_file(file_id)
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    import base64
                    data = await resp.read()
                    return base64.b64encode(data).decode("utf-8")
    except Exception as e:
        log.warning(f"Image download: {e}")
    return None


# ══════════════════════════════════════════════════════
#  ПАСХАЛКИ
# ══════════════════════════════════════════════════════
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
    """Проверяет текст на пасхалки. Возвращает ответ или None."""
    t = text.lower().strip()
    for keywords, response in EASTER_EGGS:
        if any(kw in t for kw in keywords):
            return response
    return None


def _ddg_text_sync(query: str, max_results: int = 5) -> list:
    """Синхронный текстовый поиск (вызывается через to_thread — библиотека блокирующая)."""
    try:
        return DDGS().text(query, region="ru-ru", safesearch="moderate", max_results=max_results)
    except Exception as e:
        log.warning(f"ddgs.text error: {e}")
        return []


def _ddg_news_sync(query: str, max_results: int = 5) -> list:
    """Синхронный поиск новостей (вызывается через to_thread)."""
    try:
        return DDGS().news(query, region="ru-ru", safesearch="moderate", max_results=max_results)
    except Exception as e:
        log.warning(f"ddgs.news error: {e}")
        return []


async def _ddg_search(query: str, max_results: int = 5) -> str:
    """
    Поиск в интернете — бесплатно, без API-ключей.
    Библиотека ddgs агрегирует DuckDuckGo / Bing / Brave / Yandex и т.п.
    с автоматическим фолбэком между движками, поэтому надёжнее ручного
    парсинга html.duckduckgo.com (который часто блокируется/меняется).
    """
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


# ══════════════════════════════════════════════════════
#  ПОГОДА — Open-Meteo (бесплатно, без ключа, точные данные)
#  DDG не годится для погоды (нет реалтайм-данных в выдаче),
#  поэтому отдельный канал с гарантированно точными цифрами.
# ══════════════════════════════════════════════════════
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
    """Достаёт название города из запроса о погоде ('погода в Москве' → 'Москве')."""
    t = f" {text.strip().lower()} "
    for kw in ("погодка", "погода", "погоду", "погоде", "weather",
               "температура", "температуру", "температуре"):
        t = t.replace(kw, " ")
    for w in (" в ", " на ", " по ", " какая ", " какой ", " сегодня ", " сейчас ", " завтра ",
              " прямо ", " там ", " in ", " at ", " is ", " the ", " today ", " now ", "?"):
        t = t.replace(w, " ")
    return t.strip(" ?!.,")


async def _geocode_city(session: aiohttp.ClientSession, city: str) -> Optional[dict]:
    """Ищет координаты города, с учётом русских падежей (Москве → Москва)."""
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
    """Текущая погода через Open-Meteo — бесплатно, без ключа, реальные данные на сейчас."""
    if not city:
        return None
    try:
        async with aiohttp.ClientSession() as session:
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

        # Прогноз на завтра (daily.time[1], т.к. [0] — сегодня)
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
    """
    Определяет нужен ли поиск — по ответу ИИ или по характеру вопроса.
    Используется как ПОСЛЕДНЯЯ подстраховка — после ответа модели,
    если превентивный поиск не сработал (например, триггер не совпал,
    а модель сама призналась, что не знает).
    """
    reply_lower = reply.lower()

    # Фразы когда ИИ признаётся что не знает / данные устарели
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
    # Любой год из недавнего диапазона (вместо жёстко зашитых 2024/2025/2026,
    # которые устарели бы сами по себе в 2027) — сигнал, что нужны свежие данные.
    current_year = date.today().year
    if re.search(r"\b(20[2-9]\d)\b", user_msg) and any(
        str(y) in user_msg for y in range(current_year - 2, current_year + 2)
    ):
        return True
    return False


def _needs_search_preemptive(user_msg: str) -> bool:
    """
    Проверка ДО обращения к модели — по ключевым словам в самом вопросе.
    Если сработала — ищем сразу и отдаём модели готовый контекст за один
    запрос, вместо того чтобы сперва получить неуверенный ответ модели,
    а потом переспрашивать (медленнее и менее надёжно).
    """
    t = user_msg.lower()
    if any(tr in t for tr in SEARCH_TRIGGERS):
        return True
    current_year = date.today().year
    if re.search(r"\b(20[2-9]\d)\b", user_msg) and any(
        str(y) in user_msg for y in range(current_year - 2, current_year + 2)
    ):
        return True
    return False


async def _groq_request(messages: list, max_tokens: int = 2048, temperature: float = 0.7, model: str = GROQ_MODEL) -> Optional[str]:
    """Базовый запрос к Groq API."""
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
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
                    log.error(f"Groq non-JSON (status {resp.status}): {raw[:300]}")
                    return None
                if "choices" not in data:
                    log.error(f"Groq unexpected: {data}")
                    return None
                return data["choices"][0]["message"]["content"].strip()
    except asyncio.TimeoutError:
        log.warning("Groq timeout")
        return None
    except Exception as e:
        log.error(f"Groq request: {e}")
        return None


# ══════════════════════════════════════════════════════
#  БЕЗОПАСНАЯ ОТПРАВКА HTML-ОТВЕТА ИИ
#  Модель по системному промпту сама пишет <b>/<i>/<pre><code> —
#  их нельзя экранировать html_escape (иначе теги видны как текст).
#  Но если модель случайно пришлёт кривой HTML (незакрытый тег и т.п.),
#  Telegram отклонит сообщение целиком с ошибкой "can't parse entities" —
#  тогда откатываемся на экранированный текст, чтобы ответ всё равно дошёл.
# ══════════════════════════════════════════════════════
def _normalize_code_blocks(text: str) -> str:
    """
    Страховка: модель иногда игнорирует системный промпт и всё равно шлёт код
    в Markdown (```...```) вместо <pre><code>. Конвертируем сами, чтобы
    Telegram гарантированно показал кнопку «скопировать» вне зависимости
    от того, как ответила модель.
    """
    text = re.sub(
        r"```(?:\w+)?\n?(.*?)```",
        lambda m: f"<pre><code>{html_escape(m.group(1))}</code></pre>",
        text,
        flags=re.DOTALL,
    )
    # одиночные `код` -> <code>код</code>, но не трогаем то, что уже внутри <pre><code>
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
    """
    Отправляет ответ ИИ сообщением, доверяя HTML-тегам от модели.
    use_reply=True — через msg.reply (цепочка "в ответ на", как было в группах);
    use_reply=False — через msg.answer (новое сообщение в тот же чат).
    При ошибке парсинга HTML — повторяет с экранированным текстом,
    чтобы пользователь получил хоть что-то, а не тишину.
    """
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
    """Аналог _reply_ai_html, но для edit_text (когда уже есть 'думаю...' сообщение)."""
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
    """
    Аналог для бизнес-чата (правка сообщения в чате собеседника через HTTP).
    _business_edit_message уже логирует description при ошибке — используем
    его, и при признаке кривого HTML повторяем с экранированным текстом.
    """
    text = f"{prefix}{answer}"
    ok, description = await _business_edit_message_ex(conn_id, chat_id, msg_id, text)
    if not ok and _looks_like_bad_html(description):
        log.warning(f"Business AI edit bad HTML, falling back to escaped: {description}")
        fallback = f"{prefix}{html_escape(answer)}"
        ok, _ = await _business_edit_message_ex(conn_id, chat_id, msg_id, fallback)
    return ok


async def groq_chat(uid: int, user_msg: str, image_base64: Optional[str] = None) -> str:
    """
    Отправляет сообщение в Groq.
    Для текстовых вопросов, требующих свежих данных (курсы, новости, релизы,
    погода и т.п.), поиск в DuckDuckGo запускается ПРЕВЕНТИВНО — до первого
    обращения к модели — и результаты сразу передаются как контекст.
    Так .ai работает как единое целое с .search: не нужно гадать, признается
    ли модель в незнании — если вопрос явно про «сейчас», ответ всегда живой.
    Фолбэк: если триггер не сработал, а модель всё равно ответила неуверенно —
    подключаем поиск вторым запросом (как раньше).
    image_base64 — опционально, если пользователь отправил фото.
    Llama 4 Scout понимает изображения нативно.
    """
    # Проверяем пасхалки до обращения к API
    egg = _check_easter_egg(user_msg)
    if egg:
        return egg

    history = ai_history.setdefault(uid, [])

    # Формируем контент текущего сообщения
    if image_base64:
        content = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
            },
            {
                "type": "text",
                "text": user_msg if user_msg else "Опиши что на фото."
            }
        ]
    else:
        content = user_msg

    history.append({"role": "user", "content": content})
    if len(history) > 10:
        ai_history[uid] = history[-10:]
        history = ai_history[uid]

    active_model = GROQ_MODEL if image_base64 else GROQ_MODEL_TEXT
    already_searched = False

    # ═══ Погода — отдельный точный канал, без модели вообще (только текст) ═══
    if not image_base64 and _is_weather_query(user_msg):
        city = _extract_city(user_msg)
        weather_text = await _get_weather(city) if city else None
        if weather_text:
            reply = weather_text + "\n\n◐ <i>точные данные о погоде</i>"
            ai_history[uid].append({"role": "assistant", "content": reply})
            return reply
        if city:
            reply = f"⚠️ Не нашёл город «{city}» — уточни название и спроси ещё раз."
            ai_history[uid].append({"role": "assistant", "content": reply})
            return reply
        # если города в запросе нет вовсе — идём дальше как обычный вопрос

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    # ═══ Превентивный поиск — до запроса к модели ═══
    if not image_base64 and _needs_search_preemptive(user_msg):
        log.info(f"🔍 Preemptive search for uid={uid}: {user_msg[:60]}")
        search_results = await _ddg_search(user_msg)
        if search_results:
            messages = messages + [
                {
                    "role": "user",
                    "content": (
                        f"[Результаты поиска по запросу «{user_msg}»]\n\n"
                        f"{search_results}\n\n"
                        "Используй эти данные, если они релевантны вопросу — "
                        "дай актуальный и точный ответ. Отвечай на языке "
                        "пользователя, кратко и по делу."
                    )
                }
            ]
            already_searched = True

    # Первый (или единственный) запрос к ИИ
    reply = await _groq_request(messages, model=active_model)
    if reply is None:
        return "⚠️ ИИ временно недоступен — попробуй позже."
    reply = _normalize_code_blocks(reply)
    if already_searched:
        reply += "\n\n◐ <i>ответ дополнен поиском</i>"

    # ═══ Фолбэк-поиск — только если превентивный не сработал, а модель
    # всё равно ответила неуверенно (только для текстовых запросов) ═══
    if not image_base64 and not already_searched and _needs_search(reply, user_msg):
        log.info(f"🔍 Fallback search triggered for uid={uid}: {user_msg[:60]}")
        search_results = await _ddg_search(user_msg)

        if search_results:
            augmented_messages = messages + [
                {"role": "assistant", "content": reply},
                {
                    "role": "user",
                    "content": (
                        f"[Результаты поиска по запросу «{user_msg}»]\n\n"
                        f"{search_results}\n\n"
                        "На основе этих данных дай актуальный и точный ответ. "
                        "Если информация из поиска полезна — используй её. "
                        "Отвечай на языке пользователя, кратко и по делу."
                    )
                }
            ]
            reply_with_search = await _groq_request(augmented_messages, model=active_model)
            if reply_with_search:
                reply = _normalize_code_blocks(reply_with_search) + "\n\n◐ <i>ответ дополнен поиском</i>"
                log.info(f"🔍 Search augmented reply for uid={uid}")

    # Сохраняем в историю
    ai_history[uid].append({"role": "assistant", "content": reply})
    return reply


# ══════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════
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

    is_prem = await db.is_premium(uid)
    home_text_full = (
        f"◆ <b>QUIET MOD</b> 👁️\n"
        f"<code>{LINE}</code>\n\n"
        f"<b>{html_escape(name)}</b>, добро пожаловать в тишину.\n\n"
        "Я слежу за тем, что исчезает —\n"
        "<b>удалённые и изменённые</b> сообщения\n"
        "появятся здесь раньше, чем их забудут.\n\n"
        f"<code>{LINE}</code>\n"
        f"◇ Статус       <b>{'VIP-статус' if is_prem else 'Базовый доступ'}</b>\n"
        f"◇ Перехват     <b>безлимит</b>\n"
        f"◇ Архив        <b>{'200' if is_prem else '20'} записей</b>\n"
        f"◇ ИИ           <b>без лимитов</b>\n"
        f"<code>{LINE}</code>\n\n"
        f"◇ Пригласить:\n"
        f"<code>{ref_link(uid)}</code>"
    )
    await _show_home(uid, home_text_full, kb_main(uid, is_prem), msg)


# ══════════════════════════════════════════════════════
#  /admin
# ══════════════════════════════════════════════════════
@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(
        f"▲ <b>Admin Suite</b>\n{LINE}",
        reply_markup=kb_admin(),
    )


# ══════════════════════════════════════════════════════
#  .ai КОМАНДА В БИЗНЕС-ЧАТЕ
#  Пишешь: .ai вопрос
#  Бот редактирует твоё сообщение: ⏳ → ответ + @бот
# ══════════════════════════════════════════════════════

async def _business_edit_message_ex(conn_id: str, chat_id: int, msg_id: int, text: str) -> tuple[bool, Optional[str]]:
    """
    Редактирует бизнес-сообщение напрямую через Bot API (HTTP),
    т.к. aiogram 3.7 не поддерживает business_connection_id в edit_message_text.
    Возвращает (успех, описание_ошибки_от_telegram_если_есть) — описание нужно,
    чтобы отличить кривой HTML от прочих сбоев (сеть, права и т.п.).
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "business_connection_id": conn_id,
        "chat_id": chat_id,
        "message_id": msg_id,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    description = data.get("description")
                    log.warning(f"editMessageText API error: {description}")
                    return False, description
                return True, None
    except Exception as e:
        log.warning(f"editMessageText HTTP: {e}")
        return False, str(e)


async def _business_edit_message(conn_id: str, chat_id: int, msg_id: int, text: str) -> bool:
    """Обёртка над _business_edit_message_ex для мест, где описание ошибки не нужно."""
    ok, _ = await _business_edit_message_ex(conn_id, chat_id, msg_id, text)
    return ok


@dp.business_message(F.text.regexp(r"(?i)^\.ai\s+.+"))
async def on_ai_inline(msg: Message):
    """
    Только от владельца бизнес-аккаунта.
    Редактирует сообщение прямо в чате собеседника.
    Не попадает в кэш и не вызывает уведомление об изменении.
    """
    if not msg.business_connection_id:
        return

    try:
        conn = await bot.get_business_connection(msg.business_connection_id)
        owner_id = conn.user.id
    except Exception as e:
        log.error(f"get_business_connection (.ai): {e}")
        return

    # Реагируем только на сообщения самого владельца
    if not msg.from_user or msg.from_user.id != owner_id:
        return

    # Текст после ".ai "
    raw_text = msg.text or msg.caption or ""
    question = raw_text[raw_text.index(" ") + 1:].strip() if " " in raw_text else ""

    # Шаг 1: редактируем → "ожидание..." с мигающим эффектом (~7 сек)
    ok = await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        "◆ ·"
    )
    if not ok:
        return

    await asyncio.sleep(1)
    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◆ · ·")
    await asyncio.sleep(1)
    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◆ · · ·")
    await asyncio.sleep(1)

    # Шаг 2: если есть фото — скачиваем
    image_b64 = None
    if msg.photo:
        image_b64 = await _get_image_base64(bot, msg.photo[-1].file_id)

    # Шаг 3: получаем ответ (с фото или без)
    answer = await groq_chat(owner_id, question or "Опиши что на фото.", image_base64=image_b64)

    # Шаг 4: редактируем → ответ + подпись (доверяем HTML-тегам от модели,
    # с фоллбэком на экранирование, если она пришлёт кривой HTML)
    await _business_edit_ai_html(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        prefix="", answer=f"{answer}\n\n— 👁️ @{BOT_USERNAME}"
    )
    log.info(f"🤖 .ai done owner={owner_id} chat={msg.chat.id} with_photo={image_b64 is not None}")


# ══════════════════════════════════════════════════════
#  .ai КОМАНДА В ГРУППАХ И КАНАЛАХ
#  Работает когда бот добавлен в группу/канал.
#  Пишешь: .ai вопрос  — бот отвечает в том же чате.
# ══════════════════════════════════════════════════════
@dp.message(F.text.regexp(r"(?i)^\.ai\s+.+"), F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_ai_group(msg: Message):
    """
    .ai в группах/каналах — бот отвечает обычным сообщением в том же чате.
    """
    if not msg.from_user:
        return

    uid = msg.from_user.id
    raw_text = msg.text or msg.caption or ""
    question = raw_text[raw_text.index(" ") + 1:].strip() if " " in raw_text else ""
    if not question:
        return

    # Регистрируем пользователя если ещё нет
    await db.upsert_user(uid, msg.from_user.username or "", msg.from_user.full_name or "")

    thinking = await msg.reply("◆ · · ·")

    image_b64 = None
    if msg.photo:
        image_b64 = await _get_image_base64(bot, msg.photo[-1].file_id)

    answer = await groq_chat(uid, question, image_base64=image_b64)

    try:
        await _edit_ai_html(thinking, prefix="◆ ", answer=answer)
    except Exception:
        try:
            await thinking.delete()
            await _reply_ai_html(msg, prefix="◆ ", answer=answer, use_reply=True)
        except Exception as e:
            log.error(f"ai_group reply: {e}")

    log.info(f"🤖 .ai group chat={msg.chat.id} user={uid}")


# ══════════════════════════════════════════════════════
#  .search КОМАНДА В БИЗНЕС-ЧАТЕ
#  Пишешь: .search запрос
#  Бот ищет в интернете и редактирует твоё сообщение
# ══════════════════════════════════════════════════════
@dp.business_message(F.text.regexp(r"(?i)^\.search\s+.+"))
async def on_search_inline(msg: Message):
    """Поиск в бизнес-чате — редактирует сообщение владельца."""
    if not msg.business_connection_id:
        return

    try:
        conn = await bot.get_business_connection(msg.business_connection_id)
        owner_id = conn.user.id
    except Exception as e:
        log.error(f"get_business_connection (.search): {e}")
        return

    if not msg.from_user or msg.from_user.id != owner_id:
        return

    raw_text = msg.text or ""
    query = raw_text[raw_text.index(" ") + 1:].strip() if " " in raw_text else ""
    if not query:
        return

    # Анимация ожидания
    ok = await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◐ ·")
    if not ok:
        return
    await asyncio.sleep(1)
    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◐ · ·")
    await asyncio.sleep(1)
    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "◐ · · ·")
    await asyncio.sleep(1)

    # Поиск + ответ ИИ
    if _is_weather_query(query):
        city = _extract_city(query)
        weather_text = await _get_weather(city) if city else None
        if weather_text:
            answer = weather_text
        elif city:
            answer = f"⚠️ Не нашёл город «{city}» — проверь название и попробуй ещё раз."
        else:
            answer = "🌤 Уточни город, например: .search погода в Москве"
    else:
        search_results = await _ddg_search(query)
        if search_results:
            prompt = (
                f"Пользователь ищет: «{query}»\n\n"
                f"Результаты поиска:\n{search_results}\n\n"
                "Дай чёткий и актуальный ответ на основе этих данных. Кратко, по делу."
            )
        else:
            prompt = f"Найди и расскажи всё что знаешь про: {query}"

        answer = await _groq_request([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ], model=GROQ_MODEL_TEXT)
        if not answer:
            answer = "⚠️ Не удалось получить результаты поиска — попробуй позже."
        else:
            answer = _normalize_code_blocks(answer)

    await _business_edit_ai_html(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        prefix="◐ ", answer=f"{answer}\n\n— 👁️ @{BOT_USERNAME}"
    )
    log.info(f"🔍 .search done owner={owner_id} query={query[:50]}")


# ══════════════════════════════════════════════════════
#  .search КОМАНДА В ГРУППАХ И КАНАЛАХ
# ══════════════════════════════════════════════════════
@dp.message(F.text.regexp(r"(?i)^\.search\s+.+"), F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_search_group(msg: Message):
    """.search в группах/каналах — отвечает в том же чате."""
    if not msg.from_user:
        return

    uid = msg.from_user.id
    raw_text = msg.text or ""
    query = raw_text[raw_text.index(" ") + 1:].strip() if " " in raw_text else ""
    if not query:
        return

    await db.upsert_user(uid, msg.from_user.username or "", msg.from_user.full_name or "")
    thinking = await msg.reply("◐ · · ·")

    if _is_weather_query(query):
        city = _extract_city(query)
        weather_text = await _get_weather(city) if city else None
        if weather_text:
            answer = weather_text
        elif city:
            answer = f"⚠️ Не нашёл город «{city}» — проверь название и попробуй ещё раз."
        else:
            answer = "🌤 Уточни город, например: .search погода в Москве"
    else:
        search_results = await _ddg_search(query)
        if search_results:
            prompt = (
                f"Пользователь ищет: «{query}»\n\n"
                f"Результаты поиска:\n{search_results}\n\n"
                "Дай чёткий и актуальный ответ на основе этих данных. Кратко, по делу."
            )
        else:
            prompt = f"Найди и расскажи всё что знаешь про: {query}"

        answer = await _groq_request([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ], model=GROQ_MODEL_TEXT)
        if not answer:
            answer = "⚠️ Не удалось получить результаты поиска — попробуй позже."
        else:
            answer = _normalize_code_blocks(answer)

    try:
        await _edit_ai_html(thinking, prefix="◐ ", answer=answer)
    except Exception:
        try:
            await thinking.delete()
            await _reply_ai_html(msg, prefix="◐ ", answer=answer, use_reply=True)
        except Exception as e:
            log.error(f"search_group reply: {e}")

    log.info(f"🔍 .search group chat={msg.chat.id} user={uid} query={query[:50]}")




# ══════════════════════════════════════════════════════
#  КЭШИРОВАНИЕ БИЗНЕС-СООБЩЕНИЙ
#  FIX: owner_id = business_connection_id → user.id
# ══════════════════════════════════════════════════════
@dp.business_message()
async def on_business_msg(msg: Message):
    """
    Правильный owner_id: получаем через business_connection,
    чтобы совпадал с тем, что приходит в on_deleted.
    """
    if not msg.business_connection_id:
        return

    # Не кэшируем .ai и .search команды — они будут отредактированы
    if msg.text and (msg.text.lower().startswith(".ai ") or msg.text.lower().startswith(".search ")):
        return

    try:
        conn = await bot.get_business_connection(msg.business_connection_id)
        owner_id = conn.user.id
    except Exception as e:
        log.error(f"get_business_connection (save): {e}")
        return

    media_type = "◆ Текст"
    file_id: Optional[str] = None
    for attr, label in MEDIA_MAP.items():
        obj = getattr(msg, attr, None)
        if obj:
            media_type = label
            file_id = obj[-1].file_id if attr == "photo" else (getattr(obj, "file_id", None))
            break

    await db.save_message(owner_id, {
        "msg_id":     msg.message_id,
        "sender_id":  msg.from_user.id if msg.from_user else None,
        "from_name":  msg.from_user.full_name if msg.from_user else "Неизвестно",
        "username":   f"@{msg.from_user.username}" if msg.from_user and msg.from_user.username else "",
        "chat":       msg.chat.title or getattr(msg.chat, "full_name", None) or "Личные",
        "date":       fmt_msg_date(msg.date),
        "text":       msg.text or msg.caption or "",
        "media_type": media_type,
        "file_id":    file_id,
    })
    log.info(f"📥 cached msg={msg.message_id} owner={owner_id}")


# ══════════════════════════════════════════════════════
#  ИЗМЕНЁННЫЕ БИЗНЕС-СООБЩЕНИЯ
# ══════════════════════════════════════════════════════
@dp.edited_business_message()
async def on_edited_business_msg(msg: Message):
    """
    Ловим изменения сообщений.
    Если это .ai ответ бота — тихо обновляем кэш, не уведомляем.
    Если это реальное изменение — уведомляем владельца было/стало.
    """
    if not msg.business_connection_id:
        return

    try:
        conn = await bot.get_business_connection(msg.business_connection_id)
        owner_id = conn.user.id
    except Exception as e:
        log.error(f"get_business_connection (edit): {e}")
        return

    new_text = msg.text or msg.caption or ""

    # Не уведомляем об изменениях сделанных самим ботом (.ai ответы)
    is_bot_edit = (
        f"— 👁️ @{BOT_USERNAME}" in new_text
        or new_text.strip().startswith("◆")
    )

    # Не уведомляем если владелец редактирует своё собственное сообщение
    sender_id = msg.from_user.id if msg.from_user else None
    is_owner_edit = (sender_id == owner_id)

    if not is_bot_edit and not is_owner_edit:
        cached = await db.get_message(owner_id, msg.message_id)
        old_text = cached["text"] if cached else None
        sender = fmt_sender(
            msg.from_user.full_name if msg.from_user else "Неизвестно",
            f"@{msg.from_user.username}" if msg.from_user and msg.from_user.username else "",
        )
        chat_name = msg.chat.title or getattr(msg.chat, "full_name", None) or "Личные"
        notify = (
            f"✦ <b>Сообщение отредактировано</b>\n"
            f"{LINE}\n"
            f"◇ <b>{html_escape(sender)}</b>\n"
            f"◆ {html_escape(chat_name)}\n"
            f"◷ {fmt_msg_date(msg.date)}\n"
            f"{LINE}\n"
        )
        if old_text:
            notify += f"◇ <b>Было:</b>\n{html_escape(old_text)}\n\n"
        else:
            notify += "◇ <b>Было:</b> <i>нет в архиве</i>\n\n"
        notify += f"◆ <b>Стало:</b>\n{html_escape(new_text)}"

        # Сохраняем в saved_messages для возможного сохранения пользователем
        save_id = await db.save_intercepted(owner_id, {
            "from_name":  msg.from_user.full_name if msg.from_user else "Неизвестно",
            "username":   f"@{msg.from_user.username}" if msg.from_user and msg.from_user.username else "",
            "chat":       chat_name,
            "date":       fmt_msg_date(msg.date),
            "text":       new_text,
            "media_type": "◆ Текст",
            "file_id":    None,
            "event_type": "edited",
            "old_text":   old_text,
        })
        await _send_notify(owner_id, notify, reply_markup=kb_notify(save_id))

    # В любом случае обновляем кэш новым содержимым
    media_type = "◆ Текст"
    file_id: Optional[str] = None
    for attr, label in MEDIA_MAP.items():
        obj = getattr(msg, attr, None)
        if obj:
            media_type = label
            file_id = obj[-1].file_id if attr == "photo" else (getattr(obj, "file_id", None))
            break

    await db.save_message(owner_id, {
        "msg_id":     msg.message_id,
        "sender_id":  msg.from_user.id if msg.from_user else None,
        "from_name":  msg.from_user.full_name if msg.from_user else "Неизвестно",
        "username":   f"@{msg.from_user.username}" if msg.from_user and msg.from_user.username else "",
        "chat":       msg.chat.title or getattr(msg.chat, "full_name", None) or "Личные",
        "date":       fmt_msg_date(msg.date),
        "text":       new_text,
        "media_type": media_type,
        "file_id":    file_id,
    })
    log.info(f"✏️ updated msg={msg.message_id} owner={owner_id} bot_edit={is_bot_edit}")


# ══════════════════════════════════════════════════════
#  ГОЛОС → ТЕКСТ  (Groq Whisper)
# ══════════════════════════════════════════════════════
async def _transcribe_voice(file_id: str) -> Optional[str]:
    """Скачивает голосовое сообщение и транскрибирует через Groq Whisper."""
    try:
        file = await bot.get_file(file_id)
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return None
                audio_bytes = await resp.read()

        import io
        form = aiohttp.FormData()
        form.add_field("file", io.BytesIO(audio_bytes), filename="voice.ogg", content_type="audio/ogg")
        form.add_field("model", "whisper-large-v3")
        form.add_field("response_format", "text")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                data=form,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    return (await resp.text()).strip()
    except Exception as e:
        log.warning(f"Whisper transcribe: {e}")
    return None


# ══════════════════════════════════════════════════════
#  УДАЛЁННЫЕ БИЗНЕС-СООБЩЕНИЯ
# ══════════════════════════════════════════════════════
async def _send_media(owner_id: int, file_id: str, mt: str):
    try:
        if "Фото"      in mt: await bot.send_photo(owner_id, file_id)
        elif "Видео"   in mt: await bot.send_video(owner_id, file_id)
        elif "Голос"   in mt: await bot.send_voice(owner_id, file_id)
        elif "Кружок"  in mt: await bot.send_video_note(owner_id, file_id)
        elif "Документ" in mt: await bot.send_document(owner_id, file_id)
        elif "GIF"     in mt: await bot.send_animation(owner_id, file_id)
        elif "Стикер"  in mt: await bot.send_sticker(owner_id, file_id)
    except Exception as e:
        log.warning(f"Media send: {e}")


@dp.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted):
    log.info(f"🚨 deleted conn={event.business_connection_id} ids={event.message_ids}")
    try:
        conn = await bot.get_business_connection(event.business_connection_id)
        owner_id = conn.user.id
    except Exception as e:
        log.error(f"get_business_connection (delete): {e}")
        return

    for msg_id in event.message_ids:
        cached = await db.get_message(owner_id, msg_id)
        if not cached:
            # Сообщения нет в кэше (например бот только подключился, или это
            # служебное/непойманное апдейтом сообщение) — молча пропускаем,
            # чтобы не засорять чат бесполезными уведомлениями без содержимого.
            log.info(f"❓ msg={msg_id} not in cache for owner={owner_id} — skip, nothing to show")
            continue

        # Не уведомляем о своих собственных удалённых сообщениях
        if cached.get("sender_id") == owner_id:
            log.info(f"⏭ skip own deleted msg={msg_id} owner={owner_id}")
            continue

        sender = fmt_sender(cached["from_name"], cached["username"])

        text = (
            f"✕ <b>Удалённое сообщение</b>\n"
            f"{LINE}\n"
            f"◇ <b>{sender}</b>\n"
            f"   удалил(а) сообщение\n"
            f"{LINE}\n"
            f"◆ Чат: {cached['chat']}\n"
            f"◷ Время: {cached['date']}\n"
            f"◇ Тип: {cached['media_type']}"
        )
        if cached["text"]:
            text += f"\n{LINE}\n◆ <b>Содержимое:</b>\n{cached['text']}"

        # Сохраняем в saved_messages для возможного сохранения пользователем
        save_id = await db.save_intercepted(owner_id, {
            "from_name":  cached["from_name"],
            "username":   cached["username"],
            "chat":       cached["chat"],
            "date":       cached["date"],
            "text":       cached["text"],
            "media_type": cached["media_type"],
            "file_id":    cached["file_id"],
            "event_type": "deleted",
            "old_text":   None,
        })

        sent_id = await _send_notify(owner_id, text, reply_markup=kb_notify(save_id))
        if sent_id is None:
            continue

        if cached["file_id"]:
            await _send_media(owner_id, cached["file_id"], cached["media_type"])
            # Голос → текст
            if "Голос" in cached["media_type"] and cached["file_id"]:
                transcript = await _transcribe_voice(cached["file_id"])
                if transcript:
                    try:
                        await bot.send_message(
                            owner_id,
                            f"◆ <b>Расшифровка голосового:</b>\n{LINE}\n{html_escape(transcript)}"
                        )
                    except Exception:
                        pass


# ══════════════════════════════════════════════════════
#  ИИ ЧАТ  (без лимитов — Groq бесплатный)
# ══════════════════════════════════════════════════════
@dp.callback_query(F.data == "ai_open")
async def cb_ai_open(call: CallbackQuery, state: FSMContext):
    await state.set_state(S.ai_chat)
    await call.answer()
    await call.message.edit_text(
        f"◆ <b>ИИ-консьерж</b>\n{LINE}\n"
        f"Модель: <b>Llama 4 Scout · Vision</b>\n"
        f"Лимит: <b>без ограничений</b>\n\n"
        "Спрашивай что угодно — отвечу тихо и быстро ◆",
        reply_markup=kb_ai(),
    )


# Кадры "вращения" глаза — те же дуги, что дают эффект спиннера в CLI
THINKING_FRAMES = ["◜ 👁️ Думаю", "◝ 👁️ Думаю", "◞ 👁️ Думаю", "◟ 👁️ Думаю"]
THINKING_INTERVAL = 0.4  # сек между кадрами


async def _spin_thinking(chat_id: int, message_id: int):
    """
    Фоновая анимация «глаз думает» — крутит кадры THINKING_FRAMES,
    пока задачу не отменят (asyncio.CancelledError) снаружи, когда
    ответ ИИ готов. Работает в чате с ИИ внутри бота (только там).
    """
    i = 0
    try:
        while True:
            frame = THINKING_FRAMES[i % len(THINKING_FRAMES)]
            try:
                await bot.edit_message_text(frame, chat_id=chat_id, message_id=message_id)
            except Exception:
                pass  # сообщение могли удалить/не изменилось — не критично, продолжаем крутить
            i += 1
            await asyncio.sleep(THINKING_INTERVAL)
    except asyncio.CancelledError:
        pass  # штатная отмена — ответ ИИ уже готов


@dp.message(S.ai_chat)
async def ai_msg(msg: Message, state: FSMContext):
    uid = msg.from_user.id

    # Принимаем текст, фото (с подписью или без), или фото + текст
    has_photo = bool(msg.photo)
    has_text  = bool(msg.text or msg.caption)

    if not has_text and not has_photo:
        await msg.answer("◇ Отправь текст или фото (можно с подписью).")
        return

    text_content = msg.text or msg.caption or ""

    thinking = await msg.answer(THINKING_FRAMES[0])
    spin_task = asyncio.create_task(_spin_thinking(thinking.chat.id, thinking.message_id))

    image_b64 = None
    if has_photo:
        # Берём лучшее качество (последний элемент)
        file_id = msg.photo[-1].file_id
        image_b64 = await _get_image_base64(bot, file_id)
        if image_b64 is None:
            spin_task.cancel()
            await thinking.edit_text("◇ Не смог загрузить фото — попробуй ещё раз.")
            return

    try:
        reply = await groq_chat(uid, text_content, image_base64=image_b64)
    finally:
        spin_task.cancel()

    await thinking.delete()
    await _reply_ai_html(msg, prefix="◆ ", answer=reply, reply_markup=kb_ai())


@dp.callback_query(F.data == "ai_clear")
async def cb_ai_clear(call: CallbackQuery):
    ai_history.pop(call.from_user.id, None)
    await call.answer("✕ Диалог сброшен", show_alert=True)


@dp.callback_query(F.data == "ai_exit")
async def cb_ai_exit(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid     = call.from_user.id
    is_prem = await db.is_premium(uid)
    await call.answer()
    await call.message.edit_text(
        home_text(is_prem),
        reply_markup=kb_main(uid, is_prem),
    )





# ══════════════════════════════════════════════════════
#  ПОИСК ПО КЭШУ (только premium)
# ══════════════════════════════════════════════════════
@dp.callback_query(F.data == "search")
async def cb_search(call: CallbackQuery, state: FSMContext):
    if not await db.is_premium(call.from_user.id):
        await call.answer("◈ Поиск — только для VIP", show_alert=True)
        return
    await state.set_state(S.ai_search)
    await call.answer()
    await call.message.edit_text(
        f"◐ <b>Поиск по архиву</b>\n{LINE}\n"
        "Введи имя, @username или ключевое слово:",
        reply_markup=kb_back("menu"),
    )


@dp.message(S.ai_search)
async def search_msg(msg: Message, state: FSMContext):
    if not msg.text:
        return
    await state.clear()
    uid     = msg.from_user.id
    results = await db.search_messages(uid, msg.text.strip())
    if not results:
        await msg.answer(
            f"◐ <b>Ничего не найдено</b> по «{msg.text}»",
            reply_markup=kb_back("menu"),
        )
        return
    lines = []
    for m in results[:15]:
        preview = (m["text"][:40] + "…") if len(m["text"] or "") > 40 else (m["text"] or m["media_type"])
        lines.append(f"◆ <b>{m['from_name']}</b>  {m['date']}\n   {preview}")
    await msg.answer(
        f"◐ <b>Найдено: {len(results)}</b>\n{LINE}\n" + "\n\n".join(lines),
        reply_markup=kb_back("menu"),
    )


# ══════════════════════════════════════════════════════
#  СОХРАНИТЬ НАВСЕГДА (в Saved Messages Telegram)
# ══════════════════════════════════════════════════════
@dp.callback_query(F.data.startswith("save_"))
async def cb_save_forever(call: CallbackQuery):
    """Пересылает перехваченное сообщение в Saved Messages пользователя (навсегда)."""
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
        # Отправляем в личный чат пользователя (Saved Messages = сообщение самому себе)
        await bot.send_message(uid, save_text)
        if cached["file_id"]:
            await _send_media(uid, cached["file_id"], cached["media_type"])
        await call.answer("◆ Сохранено в архиве!", show_alert=False)
        # Убираем кнопку сохранения из клавиатуры
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
        await call.answer("✕ Не удалось сохранить", show_alert=True)


# ══════════════════════════════════════════════════════
#  ОБЩИЕ CALLBACKS
# ══════════════════════════════════════════════════════
@dp.callback_query(F.data.startswith("back_"))
async def cb_back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid     = call.from_user.id
    is_prem = await db.is_premium(uid)
    await call.answer()
    await call.message.edit_text(
        home_text(is_prem),
        reply_markup=kb_main(uid, is_prem),
    )


@dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


# ══════════════════════════════════════════════════════
#  КНОПКИ ПОД УВЕДОМЛЕНИЯМИ  (nsave_ / ndel_)
# ══════════════════════════════════════════════════════
@dp.callback_query(F.data.startswith("nsave_"))
async def cb_notify_save(call: CallbackQuery):
    """Нажал «Сохранить» под уведомлением — помечаем, удаляем уведомление, возвращаем меню."""
    save_id = int(call.data.split("_")[1])
    uid     = call.from_user.id
    is_prem = await db.is_premium(uid)
    await call.answer("◆ Сохранено на 7 дней", show_alert=False)
    # Запись уже существует в saved_messages — просто удаляем уведомление
    try:
        await call.message.delete()
    except Exception:
        pass
    # Показываем главное меню заново
    existing_id = home_msg.get(uid)
    if existing_id:
        try:
            await bot.edit_message_text(
                home_text(is_prem), chat_id=uid, message_id=existing_id,
                reply_markup=kb_main(uid, is_prem), parse_mode="HTML"
            )
            return
        except Exception:
            pass
    sent = await bot.send_message(uid, home_text(is_prem), reply_markup=kb_main(uid, is_prem))
    home_msg[uid] = sent.message_id


@dp.callback_query(F.data.startswith("ndel_"))
async def cb_notify_del(call: CallbackQuery):
    """Нажал «Удалить» под уведомлением — удаляем из saved_messages, убираем уведомление, меню."""
    save_id = int(call.data.split("_")[1])
    uid     = call.from_user.id
    is_prem = await db.is_premium(uid)
    await db.delete_saved_message(save_id)
    await call.answer("✕ Удалено", show_alert=False)
    try:
        await call.message.delete()
    except Exception:
        pass
    # Показываем главное меню заново
    existing_id = home_msg.get(uid)
    if existing_id:
        try:
            await bot.edit_message_text(
                home_text(is_prem), chat_id=uid, message_id=existing_id,
                reply_markup=kb_main(uid, is_prem), parse_mode="HTML"
            )
            return
        except Exception:
            pass
    sent = await bot.send_message(uid, home_text(is_prem), reply_markup=kb_main(uid, is_prem))
    home_msg[uid] = sent.message_id


# ══════════════════════════════════════════════════════
#  СОХРАНЁННЫЕ СООБЩЕНИЯ (7 дней)
# ══════════════════════════════════════════════════════
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
        # Считаем сколько дней осталось
        from datetime import datetime as _dt
        try:
            days_left = (_dt.fromisoformat(item["expires_at"]) - _dt.now()).days + 1
        except Exception:
            days_left = 7
        lines.append(
            f"{icon} <b>{html_escape(item['from_name'] or '?')}</b>  {item['date']}\n"
            f"   {html_escape(preview)}  <i>({days_left} д.)</i>"
        )

    # Кнопки: удалить конкретное или очистить все
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
    is_prem = await db.is_premium(uid)
    await call.message.edit_text(
        home_text(is_prem),
        reply_markup=kb_main(uid, is_prem),
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
        f"◆ <b>Подключение к профилю (Business)</b>\n{LINE}\n"
        "Для этого нужен <b>Telegram Business</b> (платная подписка).\n\n"
        "1️⃣ Открой <b>Настройки Telegram</b>\n"
        "2️⃣ Перейди в <b>Telegram Business</b>\n"
        "3️⃣ Нажми <b>Автоматизация чатов</b>\n"
        f"4️⃣ Выбери <code>@{BOT_USERNAME}</code>\n"
        "5️⃣ Включи <b>Доступ к сообщениям</b>\n"
        f"{LINE}\n"
        "✔ Бот будет тихо перехватывать <b>все</b> удалённые\n"
        "и изменённые сообщения в твоих личных чатах.\n\n"
        "◇ <i>Твои собственные удалённые сообщения\n"
        "бот не присылает — только чужие.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="howto")],
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
        f"Пригласи близких — помоги проекту расти.\n\n"
        f"◇ Твоя ссылка:\n<code>{ref_link(uid)}</code>\n\n"
        f"◆ Приглашено: <b>{refs}</b>\n\n"
        "Доступ остаётся бесплатным для всех —\n"
        "приглашения помогают развивать проект.",
        reply_markup=kb_back("menu"),
    )


@dp.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery):
    uid     = call.from_user.id
    is_prem = await db.is_premium(uid)
    cached  = await db.count_messages(uid)
    refs    = await db.count_referrals(uid)
    user    = await db.get_user(uid)
    badge   = premium_badge(is_prem, bool(user and user.get("donor_badge")))
    prem_txt = user["premium_until"] if user and user.get("premium_until") else "нет"

    await call.answer()
    await call.message.edit_text(
        f"◆ <b>Твой профиль</b> {badge}\n{LINE}\n"
        f"◇ В архиве:     <b>{cached}</b>\n"
        f"◇ Приглашено:   <b>{refs}</b>\n"
        f"◇ ИИ:           <b>безлимит</b>\n"
        f"◈ VIP до:        <b>{prem_txt}</b>\n"
        f"{LINE}\n"
        f"Лимит архива: {'200 (VIP)' if is_prem else '20 (базовый)'}",
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
    is_prem = await db.is_premium(uid)
    lines = []
    for m in messages:
        preview = (m["text"][:40] + "…") if len(m["text"] or "") > 40 else (m["text"] or m["media_type"])
        lines.append(f"◆ <b>{m['from_name']}</b>  {m['date']}\n   {preview}")
    await call.answer()
    # Архив: только контекстные кнопки + назад
    archive_rows = []
    if is_prem:
        archive_rows.append([InlineKeyboardButton(text="◐ Поиск по архиву", callback_data="search")])
    archive_rows.append([InlineKeyboardButton(text="✕ Очистить архив", callback_data="clear_cache")])
    archive_rows.append([InlineKeyboardButton(text="← В меню", callback_data="back_menu")])
    await call.message.edit_text(
        f"▣ <b>Последние {len(messages)} записей</b>\n{LINE}\n" + "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=archive_rows),
    )


@dp.callback_query(F.data.startswith("ack_"))
async def cb_ack(call: CallbackQuery):
    uid     = call.from_user.id
    is_prem = await db.is_premium(uid)
    await call.answer("✔ Принято")
    await call.message.edit_text(
        home_text(is_prem),
        reply_markup=kb_main(uid, is_prem),
    )


@dp.callback_query(F.data.startswith("del_"))
async def cb_del(call: CallbackQuery):
    msg_id  = int(call.data.split("_")[1])
    uid     = call.from_user.id
    is_prem = await db.is_premium(uid)
    await db.delete_message(uid, msg_id)
    await call.answer("✕ Удалено из архива")
    await call.message.edit_text(
        home_text(is_prem),
        reply_markup=kb_main(uid, is_prem),
    )


# ══════════════════════════════════════════════════════
#  PREMIUM & DONATES
# ══════════════════════════════════════════════════════
@dp.callback_query(F.data == "premium_info")
async def cb_premium_info(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"◈ <b>VIP — что даёт?</b>\n{LINE}\n"
        "◇ <b>Бесплатно навсегда:</b>\n"
        "  • Перехват удалённых и изменённых — безлимит\n"
        "  • Архив: 20 записей\n"
        "  • ИИ: безлимитно\n\n"
        "◈ <b>VIP · 50⭐/месяц:</b>\n"
        "  • Архив: 200 записей\n"
        "  • Поиск по всему архиву\n\n"
        "◇ <b>Вклад 100⭐+ (единоразово):</b>\n"
        "  • Метка в профиле\n"
        "  • +30 дней VIP в подарок\n"
        "  • Моя искренняя благодарность",
        reply_markup=kb_premium(),
    )


@dp.callback_query(F.data.startswith("pay_"))
async def cb_pay(call: CallbackQuery):
    parts = call.data.split("_")
    kind  = parts[1]
    stars = int(parts[2])

    if kind == "premium":
        title       = "◈ VIP · 1 месяц"
        description = "VIP-доступ к Quiet Mod на 30 дней"
    else:
        title       = f"◇ Вклад {stars}⭐"
        description = f"Поддержка проекта Quiet Mod — {stars} звёзд"

    await call.answer()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=title,
        description=description,
        payload=f"{kind}_{stars}",
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
    kind = payload.split("_")[0]

    if kind == "premium":
        user    = await db.get_user(uid)
        current = user["premium_until"] if user and user.get("premium_until") else None
        if current and date.fromisoformat(current) >= date.today():
            new_date = date.fromisoformat(current) + timedelta(days=30)
        else:
            new_date = date.today() + timedelta(days=30)
        await db.set_premium(uid, new_date)
        text = (
            f"◈ <b>VIP активирован!</b>\n{LINE}\n"
            f"Действует до: <b>{new_date.strftime('%d.%m.%Y')}</b>\n"
            "Архив расширен до 200 · Поиск включён."
        )
    else:
        if stars >= DONOR_BADGE_MIN:
            await db.set_donor_badge(uid)
            bonus_date = date.today() + timedelta(days=30)
            await db.set_premium(uid, bonus_date)
            text = (
                f"◇ <b>Спасибо за поддержку!</b>\n{LINE}\n"
                f"Ты отправил <b>{stars}⭐</b>\n"
                f"Метка в профиле: ◇\n"
                f"VIP в подарок до: <b>{bonus_date.strftime('%d.%m.%Y')}</b>"
            )
        else:
            text = (
                f"◆ <b>Огромное спасибо!</b>\n{LINE}\n"
                f"Ты поддержал проект на <b>{stars}⭐</b>\n"
                "Эти средства идут на серверы и развитие."
            )

    await msg.answer(text, reply_markup=kb_back("menu"))

    try:
        await bot.send_message(
            ADMIN_ID,
            f"◈ <b>Оплата</b> · {payload}\n"
            f"◇ {msg.from_user.full_name} (ID: {uid})\n"
            f"⭐ {stars} звёзд",
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════
#  ADMIN CALLBACKS
# ══════════════════════════════════════════════════════
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


@dp.callback_query(F.data == "adm_stats")
async def cb_adm_stats(call: CallbackQuery):
    if not _is_admin(call): return
    users   = await db.count_users()
    msgs    = await db.total_messages_all()
    stars   = await db.total_stars()
    ideas   = await db.count_ideas()
    await call.answer()
    await call.message.edit_text(
        f"◆ <b>Общая статистика</b>\n{LINE}\n"
        f"◇ Пользователей:  <b>{users}</b>\n"
        f"◇ Записей в БД:   <b>{msgs}</b>\n"
        f"⭐ Всего звёзд:    <b>{stars}</b>\n"
        f"✦ Предложений:    <b>{ideas}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="adm")],
        ]),
    )


# ══════════════════════════════════════════════════════
#  ADMIN — ИДЕИ
# ══════════════════════════════════════════════════════
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
    for idea in ideas[:10]:  # показываем первые 10
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
    # обновляем список
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


# ══════════════════════════════════════════════════════
#  СООБЩЕНИЕ ВСЕМ (broadcast)
# ══════════════════════════════════════════════════════
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
        await asyncio.sleep(0.05)  # не спамим Telegram API

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


# ══════════════════════════════════════════════════════
#  ПРЕДЛОЖИТЬ ИДЕЮ (пользователь)
# ══════════════════════════════════════════════════════
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

    is_prem = await db.is_premium(uid)
    await msg.answer(
        f"✦ <b>Спасибо за идею!</b>\n{LINE}\n\n"
        "Твоё предложение отправлено разработчику.\n"
        "Лучшие идеи попадают в следующие обновления.\n\n"
        "Ты помогаешь сделать Quiet Mod лучше.",
        reply_markup=kb_back("menu"),
    )

    # Уведомляем админа
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


# ══════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════
DEVLOG = (
    "◆ <b>QUIET MOD</b> 👁️  <code>Black Edition</code>\n"
    f"{LINE}\n\n"
    "Привет! Это краткий обзор того, что умеет бот.\n"
    "Если ты здесь впервые — добро пожаловать в тишину.\n\n"
    f"{LINE}\n"
    "▲ <b>ПЕРЕХВАТ СООБЩЕНИЙ</b>\n\n"
    "✕ <b>Удалённые сообщения</b>\n"
    "   Кто-то удалил сообщение в переписке?\n"
    "   Бот мгновенно пришлёт тебе его содержимое:\n"
    "   текст, фото, видео, голосовое, стикер, GIF.\n\n"
    "✦ <b>Изменённые сообщения</b>\n"
    "   Отредактировали сообщение после отправки?\n"
    "   Увидишь сразу — что <i>было</i> и что <i>стало</i>.\n\n"
    "◇ <b>Умный фильтр</b>\n"
    "   Свои удалённые и изменённые — тишина.\n"
    "   Только чужие. Никакого лишнего шума.\n\n"
    f"{LINE}\n"
    "◆ <b>ИИ-КОНСЬЕРЖ</b>  <i>(без лимитов)</i>\n\n"
    "◇ <b>Чат с ИИ прямо в боте</b>\n"
    "   Задай любой вопрос — ИИ ответит чётко и быстро.\n"
    "   История диалога сохраняется до сброса.\n\n"
    "◇ <b>Анализ изображений</b>\n"
    "   Прикрепи фото — ИИ разберёт, прочитает текст,\n"
    "   решит задачу или объяснит что на картинке.\n\n"
    "◇ <b>ИИ в группах и каналах</b>\n"
    "   Добавь бота в любой чат, напиши:\n"
    "   <code>.ai вопрос</code> — бот ответит прямо в беседе.\n\n"
    "◇ <b>ИИ в бизнес-переписке</b>\n"
    "   Напиши <code>.ai вопрос</code> прямо в чате с собеседником —\n"
    "   бот незаметно заменит твоё сообщение ответом.\n\n"
    "◇ <b>Расшифровка голосовых</b>\n"
    "   Удалённое голосовое автоматически расшифруется\n"
    "   в текст. Whisper AI — точность 95%+.\n\n"
    f"{LINE}\n"
    "▣ <b>АРХИВ СООБЩЕНИЙ</b>\n\n"
    "◇ <b>Хранилище перехватов</b>\n"
    "   Все перехваченные сообщения хранятся в архиве.\n"
    "   Базовый: 20 записей · VIP: 200 записей.\n\n"
    "◐ <b>Поиск по архиву</b>  <i>(VIP)</i>\n"
    "   Найди любое сообщение по тексту, имени\n"
    "   отправителя или юзернейму за секунды.\n\n"
    "◆ <b>Сохранить навсегда</b>\n"
    "   Одна кнопка под уведомлением — и сообщение\n"
    "   останется у тебя навсегда вне зависимости от архива.\n\n"
    f"{LINE}\n"
    "◈ <b>VIP</b>  <code>50 звёзд / месяц</code>\n\n"
    "   • Архив расширяется с 20 до <b>200</b> записей\n"
    "   • Поиск по всему архиву\n"
    "   • Метка ◈ в профиле\n\n"
    "◇ <b>ВКЛАД</b>  <code>100+ звёзд</code>\n\n"
    "   • Метка ◇ навсегда\n"
    "   • +30 дней VIP в подарок\n"
    "   • Поддержка независимого проекта\n\n"
    f"{LINE}\n"
    "⚙ <b>КАК ПОДКЛЮЧИТЬ?</b>\n\n"
    "   Нужен <b>Telegram Business</b> (или просто добавить\n"
    "   бота в группу для ИИ-функций).\n"
    "   В боте есть кнопка <b>«Подключение»</b> — там\n"
    "   пошаговая инструкция с картинками.\n\n"
    f"{LINE}\n"
    "▲ <b>ВПЕРЕДИ — ЕЩЁ БОЛЬШЕ</b>\n\n"
    "   Бот активно развивается. В планах:\n"
    "   — Уведомления о скриншотах\n"
    "   — Статистика активности чатов\n"
    "   — Экспорт архива в файл\n"
    "   — Ещё больше ИИ-возможностей\n\n"
    "◇ Есть идея? Нажми кнопку <b>«✦ Предложить»</b> в боте.\n"
    "   Лучшие идеи от вас — уже в следующем обновлении.\n\n"
    f"{LINE}\n"
    "Спасибо что ты здесь. Это только начало.\n"
    "— Команда <b>Quiet Mod</b> 👁️"
)


async def _broadcast_devlog():
    """Рассылает DevLog всем пользователям."""
    ids = await db.all_user_ids()
    ok = 0
    fail = 0
    for uid in ids:
        try:
            await bot.send_message(uid, DEVLOG)
            ok += 1
            await asyncio.sleep(0.05)  # не спамим Telegram API
        except Exception:
            fail += 1
    log.info(f"📢 DevLog разослан: ok={ok} fail={fail}")
    try:
        await bot.send_message(ADMIN_ID, f"▤ DevLog разослан: ✔ {ok} · ✕ {fail}")
    except Exception:
        pass


PURGE_INTERVAL_SECONDS = 6 * 60 * 60  # раз в 6 часов


async def _purge_loop():
    """
    Фоновая задача: раз в PURGE_INTERVAL_SECONDS чистит истёкшие saved_messages.
    Раньше purge_expired_saved() вызывался только один раз при старте — на
    процессе, который живёт неделями без рестартов, таблица только росла.
    """
    while True:
        try:
            await asyncio.sleep(PURGE_INTERVAL_SECONDS)
            await db.purge_expired_saved()
            log.info("🧹 Просроченные saved_messages очищены")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"purge_loop: {e}")


async def main():
    await db.init_db()
    await db.purge_expired_saved()
    log.info("🚀 Quiet Mod 👁️ запускается...")
    try:
        await bot.send_message(
            ADMIN_ID,
            f"✔ <b>Бот запущен</b> · Quiet Mod 👁️ · SQLite · Railway\n"
            f"◇ Модель: Llama 4 Scout (Vision)"
        )
    except Exception:
        pass

    purge_task = asyncio.create_task(_purge_loop())

    # Корректное завершение по SIGTERM (Railway шлёт его при рестарте/деплое) —
    # без этого dp.start_polling может быть убит жёстко, не дав закрыть
    # соединение с БД (риск повреждения WAL-файла SQLite).
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(*_):
        log.info("🛑 Получен сигнал остановки — завершаю polling...")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            pass  # некоторые платформы (Windows) не поддерживают signal handlers в event loop

    polling_task = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    )
    stop_wait_task = asyncio.create_task(stop_event.wait())

    try:
        await asyncio.wait(
            {polling_task, stop_wait_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        purge_task.cancel()
        if not polling_task.done():
            await dp.stop_polling()
            polling_task.cancel()
        for t in (purge_task, polling_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        # Закрываем соединение с БД при остановке (Ctrl+C, рестарт деплоя и т.п.)
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
