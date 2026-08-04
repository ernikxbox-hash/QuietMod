import asyncio
import signal
import database as db
from core import ADMIN_ID, bot, close_http, dp, log
from tasks import _purge_loop
import handlers

async def _sync_bot_chats():
    chats = await db.get_all_bot_chats()
    if not chats:
        log.info("📌 Нет сохранённых чатов — бот подхватит их при первом сообщении в группе")
        return
    sem = asyncio.Semaphore(8)
    async def _check(chat):
        async with sem:
            try:
                member = await bot.get_chat_member(chat["id"], bot.id)
                if member.status in ("left", "kicked"):
                    await db.remove_bot_chat(chat["id"])
                    log.info(f"🧹 Бот больше не в чате {chat['id']} ({chat.get('title', '?')}) — удалён из БД")
            except Exception:
                await db.remove_bot_chat(chat["id"])
                log.info(f"🧹 Чат {chat['id']} недоступен — удалён из БД")
    await asyncio.gather(*(_check(c) for c in chats))
    log.info(f"📌 Синхронизация чатов завершена: {len(chats)} проверено")

async def main():
    await db.init_db()
    await db.purge_expired_saved()
    await db.record_stat("launch")
    log.info("📊 Статистика: запуск зафиксирован")
    await _sync_bot_chats()
    log.info("🚀 Quiet Mod 👁️ запускается...")
    try:
        await bot.send_message(
            ADMIN_ID,
            f"✔ <b>Бот запущен</b> · Quiet Mod 👁️ · SQLite · Railway\n"
            f"◇ Модель: GPT-OSS 120B"
        )
    except Exception:
        pass
    purge_task = asyncio.create_task(_purge_loop())
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    def _request_stop(*_):
        log.info("🛑 Получен сигнал остановки — завершаю polling...")
        stop_event.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            pass
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
        await db.close_db()
        await close_http()
if __name__ == "__main__":
    asyncio.run(main())
