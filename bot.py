








import asyncio

import signal



import database as db



from core import ADMIN_ID, bot, dp, log

from tasks import _purge_loop

import handlers



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





if __name__ == "__main__":

    asyncio.run(main())
