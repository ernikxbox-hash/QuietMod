import asyncio
import glob
import os
import time
import database as db
from core import log

PURGE_INTERVAL_SECONDS = 6 * 60 * 60
MEDIA_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _purge_old_media_files():
    """Удаляет локально сохранённые медиа-файлы старше 7 дней."""
    try:
        cutoff = time.time() - MEDIA_MAX_AGE_SECONDS
        for path in glob.glob("data/saved_media/*"):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except Exception:
                pass
    except Exception as e:
        log.error(f"purge media files: {e}")


async def _purge_loop():
    while True:
        try:
            await asyncio.sleep(PURGE_INTERVAL_SECONDS)
            await db.purge_expired_saved()
            _purge_old_media_files()
            log.info("🧹 Просроченные saved_messages и старые медиа очищены")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"purge_loop: {e}")
