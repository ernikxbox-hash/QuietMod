import asyncio

import logging

import os

import re

import signal

from datetime import date, datetime, timedelta, timezone

from typing import Optional



import aiohttp

from ddgs import DDGS

from aiogram import Bot, Dispatcher, F

from aiogram.client.default import DefaultBotProperties

from aiogram.enums import ParseMode

from aiogram.exceptions import TelegramRetryAfter

from aiogram.filters import Command, CommandStart, StateFilter

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



from core import (
    ADMIN_ID,
    BOT_TOKEN,
    BOT_USERNAME,
    BRAND_NAME,
    DONOR_BADGE_MIN,
    GROQ_API_KEY,
    GROQ_MODEL,
    GROQ_MODEL_TEXT,
    PREMIUM_MONTHLY_STARS,
    S,
    bot,
    log,
)





ai_history: dict[int, list] = {}



spam_tasks: dict[tuple[int, int], asyncio.Task] = {}



business_spam_tasks: dict[tuple[str, int, int], asyncio.Task] = {}



business_muted_chats: set[tuple[str, int]] = set()



business_afk: dict[str, dict] = {}

business_afk_last_reply: dict[tuple[str, int], float] = {}



business_code_mode: set[str] = set()



business_wbl_chats: set[tuple[str, int]] = set()



PROFANITY_RE = re.compile(

    r"(?iu)\b("

    r"хуй|хуе|хуя|хуи|хуё|хуйн|хуесос|хер|херн|пизд|пезд|бля|бляд|еба|ёба|ебл|уеб|уёб|"

    r"заеб|заёб|выеб|выёб|сука|сучк|мраз|гандон|пидор|пидр|педик|ублюд|мудак|мудил|"

    r"чмо|дыряв|тупоголов|нищ|обоссан|оссан|даун|плешив|дебил|идиот|твар|шлюх|проститут|дроч|"

    r"нахуй|оху|аху|"

    r"fuck|shit|bitch|cunt"

    r")\w*\b"

)







last_notify_msg: dict[int, int] = {}







home_msg: dict[int, int] = {}











class S(StatesGroup):

    ai_chat      = State()

    ai_search    = State()

    suggest_idea = State()

    broadcast    = State()











LINE = "──────────────────"





MSK = timezone(timedelta(hours=3))





def fmt_msg_date(dt) -> str:



    return dt.astimezone(MSK).strftime("%d.%m.%Y · %H:%M")





def ref_link(uid: int) -> str:

    return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"





def _fmt_duration_ru(seconds: int) -> str:

    if seconds < 60:

        return f"{seconds} сек."

    minutes = seconds // 60

    if minutes < 60:

        return f"{minutes} мин."

    hours = minutes // 60

    if hours < 24:

        return f"{hours} ч."

    days = hours // 24

    return f"{days} дн."





def _contains_profanity(text: str) -> bool:

    if not text:

        return False

    return bool(PROFANITY_RE.search(text))





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



    if username:

        return f"{from_name} ({username})"

    return from_name





def home_text(is_prem: bool) -> str:







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





async def _send_notify(owner_id: int, text: str, reply_markup=None) -> Optional[int]:









    old_id = last_notify_msg.get(owner_id)

    if old_id:

        try:

            await bot.delete_message(owner_id, old_id)

        except Exception:

            pass

        last_notify_msg.pop(owner_id, None)



    try:

        sent = await bot.send_message(owner_id, text, reply_markup=reply_markup)

        last_notify_msg[owner_id] = sent.message_id

        return sent.message_id

    except Exception as e:

        log.error(f"send notify to owner={owner_id}: {e}")

        return None











def kb_main(uid: int, is_prem: bool) -> InlineKeyboardMarkup:

    rows = []

    if uid == ADMIN_ID:

        rows.append([InlineKeyboardButton(text="▲ Admin Suite", callback_data="adm")])



    rows.append([

        InlineKeyboardButton(text="▣ Архив",        callback_data="show_all"),

        InlineKeyboardButton(text="◆ Профиль",      callback_data="stats"),

    ])

    rows.append([

        InlineKeyboardButton(text="◈ Сохранённые ➩", callback_data="show_saved"),

    ])



    if is_prem:

        rows.append([InlineKeyboardButton(text="◐ Поиск по архиву", callback_data="search")])



    rows.append([InlineKeyboardButton(text="◆ ИИ-консьерж — без лимитов", callback_data="ai_open")])



    rows.append([

        InlineKeyboardButton(text="⟡ Приглашения", callback_data="referrals"),

        InlineKeyboardButton(text="✕ Очистить",    callback_data="clear_cache"),

    ])

    rows.append([

        InlineKeyboardButton(text="✦ Предложить",   callback_data="suggest_idea"),

        InlineKeyboardButton(text="⚙ Подключение",  callback_data="howto"),

    ])



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





async def _groq_request(messages: list, max_tokens: int = 2048, temperature: float = 0.7, model: str = GROQ_MODEL) -> Optional[str]:



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





async def groq_chat(uid: int, user_msg: str, image_base64: Optional[str] = None) -> str:
















    egg = _check_easter_egg(user_msg)

    if egg:

        return egg



    history = ai_history.setdefault(uid, [])





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





    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history





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





    reply = await _groq_request(messages, model=active_model)

    if reply is None:

        return "⚠️ ИИ временно недоступен — попробуй позже."

    reply = _normalize_code_blocks(reply)

    if already_searched:

        reply += "\n\n◐ <i>ответ дополнен поиском</i>"







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





    ai_history[uid].append({"role": "assistant", "content": reply})

    return reply
