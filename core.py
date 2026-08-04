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
