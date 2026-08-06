import logging
import os
import aiohttp
from typing import Optional
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
GROQ_API_KEY  = os.environ["GROQ_API_KEY"]
GROQ_API_KEY2 = os.environ.get("GROQ_API_KEY2", "")
GROQ_API_KEY3 = os.environ.get("GROQ_API_KEY3", "")  # аварийный ключ (на случай исчерпания лимитов 1 и 2)
GROQ_API_KEYS = [k for k in (GROQ_API_KEY, GROQ_API_KEY2, GROQ_API_KEY3) if k and k.strip()]
BOT_USERNAME = "Quiet_Mod_Bot"

def _get_env(name: str, default: str = "") -> str:
    """Чтение env-переменной без чувствительности к регистру имени.

    Railway отдаёт переменные как есть: RAMKA_URL и Ramka_URL — это РАЗНЫЕ
    переменные (Linux, регистр важен). Чтобы пользователь не мог ошибиться
    регистром, ищем точное имя, а при отсутствии — совпадение без учёта
    регистра (ramka_url, Ramka_URL, RAMKA_Url и т.п.).
    """
    val = os.environ.get(name)
    if val is not None:
        return val
    for k, v in os.environ.items():
        if k.lower() == name.lower():
            return v
    return default

# Ссылка на PNG-рамку для .ramka (прозрачное отверстие по центру) — например,
# raw-файл на GitHub. Пусто → рамка рисуется кодом (барочный стиль).
# Имя переменной в Railway: RAMKA_URL (регистр больше не важен, см. _get_env).
RAMKA_URL = _get_env("RAMKA_URL")
# 🛡 Гейт подписки: доступ к боту — только подписчикам канала.
# CHANNEL_USERNAME — юзернейм канала БЕЗ @ (пустая строка = гейт выключен).
# CHANNEL_URL — ссылка, на которую ведёт кнопка «Подписаться на канал».
# CHANNEL_ID — (необязательно) числовой ID канала (например -1004482144931):
# надёжнее юзернейма — не зависит от смены @username.
CHANNEL_USERNAME = _get_env("CHANNEL_USERNAME", "Official_QuietMod").lstrip("@").strip()
CHANNEL_URL = _get_env("CHANNEL_URL", "https://t.me/Official_QuietMod").strip()
CHANNEL_ID = _get_env("CHANNEL_ID", "").strip()
# Единая модель для всего бота: флагман Groq (120B, Production) — дешевле и быстрее
# llama-3.3-70b (задепрекейчена 16.08.26). Groq сам рекомендует её как замену.
# Распознавание фото ОТКЛЮЧЕНО: llama-4-maverick (09.03.26), llama-4-scout (17.07.26)
# и qwen-vision задепрекейчены — бот работает только на текстовой модели.
GROQ_MODEL = "openai/gpt-oss-120b"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

class S(StatesGroup):
    ai_chat = State()
    ai_search = State()
    suggest_idea = State()
    broadcast = State()
    broadcast_groups = State()
    ramka = State()
    stik = State()
    krom = State()

_http_session: Optional[aiohttp.ClientSession] = None

def get_http() -> aiohttp.ClientSession:
    """Общая aiohttp-сессия: переиспользует соединения вместо TLS-рукопожатия на каждый запрос."""
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
    return _http_session

async def close_http():
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
        _http_session = None
