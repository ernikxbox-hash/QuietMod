import asyncio

import database as db

from core import log

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

