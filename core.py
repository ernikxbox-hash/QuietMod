import logging
import os
import aiohttp
from typing import Optional
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
GROQ_API_KEY  = os.environ.get("GROQ_API_KEY", "")
GROQ_API_KEY2 = os.environ.get("GROQ_API_KEY2", "")
GROQ_API_KEYS = [k for k in (GROQ_API_KEY, GROQ_API_KEY2) if k and k.strip()]
BOT_USERNAME = "Quiet_Mod_Bot"
GROQ_MODEL = "llama-3.2-90b-vision-preview"
GROQ_MODEL_TEXT = "llama-3.2-90b-vision-preview"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")
if BOT_TOKEN:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
else:
    bot = Bot(token="123456:ABC", default=DefaultBotProperties(parse_mode=ParseMode.HTML))

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
