import asyncio
import database as db
from core import log

PURGE_INTERVAL_SECONDS = 6 * 60 * 60
SUB_VERIFY_INTERVAL_SECONDS = 6 * 60 * 60  # сверка реальных пользователей (подписка на канал)

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


async def _sub_verify_loop():
    """Фоновая сверка подписок: реальные пользователи = подписчики канала гейта.

    В таблицу users попадают все, кого бот хоть раз видел (включая случайных
    участников групп) — поэтому счётчик «пользователей» врёт. Эта задача раз в
    6 часов проверяет каждого юзера через getChatMember и помечает subscribed.
    Дашборд показывает только реальных.
    """
    # Первая проверка — через минуту после старта (чтобы дашборд сразу показал
    # реальных пользователей, а не «0» на ближайшие 6 часов), дальше — по таймеру.
    await asyncio.sleep(60)
    while True:
        try:
            ok, bad = await db.verify_all_subscriptions()
            log.info(f"👁 Проверка подписок: подписаны {ok}, не подписаны {bad}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"sub_verify_loop: {e}")
        await asyncio.sleep(SUB_VERIFY_INTERVAL_SECONDS)
