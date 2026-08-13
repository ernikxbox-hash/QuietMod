"""Шуточная команда .fdox с заведомо вымышленными данными.

Команда не использует данные пользователя, Telegram API или внешние источники.
Все значения генерируются локально и специально имеют невалидный формат.
"""
import secrets
from html import escape as html_escape

from aiogram import F
from aiogram.types import Message

from business_api import _business_send_message_ex, _get_owner_id_cached
from core import BOT_USERNAME, dp, log
from functions import LINE


_FICTIONAL_NAMES = (
    "Иван Фейкович Понарошку",
    "Роман Вымышленный Бутафорский",
    "Алекс Пиксельный Несуществующий",
    "Марк Фиктивный Параллельный",
    "Степан Придуманный Ненастоящий",
)

_FICTIONAL_GEOS = (
    "г. Луноград, ул. Параллельная, д. 404",
    "п. Пиксельный, пер. Бутафорский, д. 0",
    "г. Невскрин, ул. Фиктивная, д. 13",
    "с. Понарошку, пр-т Несуществующий, д. 7",
    "г. Макетный, ул. Нарисованная, д. 999",
)


def _fake_digits(length: int) -> str:
    """Возвращает случайную последовательность цифр заданной длины."""
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def _build_fdox_text() -> str:
    """Собирает карточку только с явно невалидными тестовыми значениями."""
    name = secrets.choice(_FICTIONAL_NAMES)
    geo = secrets.choice(_FICTIONAL_GEOS)
    phone = f"+7 000 000-{_fake_digits(2)}-{_fake_digits(2)}"
    passport = f"FAKE-{_fake_digits(4)}-{_fake_digits(6)}"
    snils = f"FAKE-{_fake_digits(3)}-{_fake_digits(3)}-{_fake_digits(3)}"

    return (
        "⚠️ <b>ФЕЙК-ДОКС — ПРИКОЛ</b>\n"
        f"<code>{LINE}</code>\n\n"
        "Все данные ниже вымышлены и специально невалидны.\n"
        "Они не относятся к реальным людям или документам.\n\n"
        f"◇ <b>ГЕО:</b> {html_escape(geo)}\n"
        f"◇ <b>ФИО:</b> {html_escape(name)}\n"
        f"◇ <b>Телефон:</b> <code>{phone}</code>\n"
        f"◇ <b>Паспорт:</b> <code>{passport}</code>\n"
        f"◇ <b>СНИЛС:</b> <code>{snils}</code>\n\n"
        "⛔ Не использовать как настоящие персональные данные.\n"
        f"<code>{LINE}</code>\n"
        f"— 👁️ @{BOT_USERNAME}"
    )


# ── Обычные личные чаты и группы ────────────────────────────────────────
@dp.message(
    F.text.regexp(r"(?i)^\.fdox\s*$"),
    F.chat.type.in_({"private", "group", "supergroup"}),
)
async def on_fdox(msg: Message):
    await msg.reply(_build_fdox_text())
    log.info(f"🎭 .fdox chat={msg.chat.id} user={msg.from_user.id if msg.from_user else '?'}")


# ── Telegram Business-чаты ──────────────────────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.fdox\s*$"))
async def on_fdox_business(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id or not msg.from_user:
        return
    owner_id = await _get_owner_id_cached(conn_id, ".fdox")
    if owner_id is None or msg.from_user.id != owner_id:
        return
    await _business_send_message_ex(
        conn_id,
        msg.chat.id,
        _build_fdox_text(),
        parse_mode="HTML",
    )
    log.info(f"🎭 .fdox business chat={msg.chat.id} owner={owner_id}")