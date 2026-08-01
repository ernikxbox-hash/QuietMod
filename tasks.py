import asyncio
import database as db
from core import log

PURGE_INTERVAL_SECONDS = 6 * 60 * 60

async def _purge_loop():
    while True:
        try:
            await asyncio.sleep(PURGE_INTERVAL_SECONDS)
            await db.purge_expired_saved()
            log.info("🧹 Просроченные saved_messages очищены")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"purge_loop: {e}")
