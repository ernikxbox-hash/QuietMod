"""🧠 .recap — ИИ-рекап переписки из архива (только в ЛС с ботом).

Пишешь .recap — бот берёт из архива переписку (включая удалённые и
изменённые сообщения — их никто кроме тебя не видел!) за последние сутки,
ИИ сжимает её в краткий пересказ: о чём говорили, кто что писал,
договорённости. Результат приходит файлом recap.txt.

- .recap     — последние 24 часа
- .recap N   — последние N дней (1–30)

Только ЛС с ботом: рекап — личная штука, в чатах не нужен.

Как это работает: db.get_messages_since() достаёт сообщения архива за
период (хронологически), текст собирается в компактный дайджест
«[чат] @user: текст» с бюджетом _RECAP_MAX_CHARS, и уходит в Groq
(_groq_request) с системным промптом сжимателя. Ответ чистится от
<think>-блоков и кладётся в recap.txt.
"""
from datetime import datetime, timedelta

from aiogram import F
from aiogram.types import BufferedInputFile, Message

import database as db
from core import bot, dp, log
from functions import _groq_request, _strip_think_blocks

_RECAP_MAX_CHARS = 40_000   # бюджет на текст переписки для ИИ (не даём раздуть контекст)
_RECAP_MAX_DAYS = 30        # максимум периода в днях
_RECAP_MAX_TOKENS = 1024    # длина самого рекапа

_RECAP_SYSTEM = (
    "Ты — сжиматель переписки. Тебе дадут переписку из Telegram-архива "
    "(могут быть удалённые и изменённые сообщения). Составь КРАТКИЙ рекап "
    "на русском:\n"
    "— о чём говорили: 2–4 главные темы;\n"
    "— кто что писал: по каждому участнику 1–2 строки;\n"
    "— договорённости и обещания, если были;\n"
    "— стиль: сухо, по делу, без воды. Не начинай с «в этой переписке».\n"
    "Не выдумывай то, чего нет в переписке."
)


def _build_conversation(rows: list[dict]) -> str:
    """Переписка в компактный текст: «[чат] @user: текст» — по строке на сообщение."""
    lines: list[str] = []
    total = 0
    for m in rows:
        # Сообщения самого бота в архив не попадают, но на всякий случай
        # (старые данные) отсеиваем их и здесь.
        if m.get("sender_id") == bot.id:
            continue
        sender = (m.get("username") or "").strip() or (m.get("from_name") or "?")
        chat = (m.get("chat") or "").strip() or "?"
        text = (m.get("text") or "").strip()
        if not text:
            media = (m.get("media_type") or "").strip()
            text = f"[{media}]" if media else ""
        if not text:
            continue
        line = f"[{chat}] {sender}: {text}"
        total += len(line) + 1
        if total > _RECAP_MAX_CHARS:
            break
        lines.append(line)
    return "\n".join(lines)


@dp.message(F.text.regexp(r"(?i)^\.recap(\s+\d{1,3})?$"), F.chat.type == "private")
async def on_recap_cmd(msg: Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    args = (msg.text or "").strip()[len(".recap"):].strip()
    days = 1
    if args.isdigit():
        days = min(max(int(args), 1), _RECAP_MAX_DAYS)
    thinking = await msg.answer("◆ · · ·")
    try:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = await db.get_messages_since(uid, since)
        conversation = _build_conversation(rows)
        if not conversation:
            try:
                await thinking.edit_text(
                    "◇ В архиве нет сообщений за этот период — рекап не из чего сделать.\n"
                    "◇ Попробуй <code>.recap 7</code> — за неделю."
                )
            except Exception:
                pass
            return
        prompt = (
            f"Переписка за последние {days} дн. ({len(rows)} сообщений из архива):\n\n"
            f"{conversation}"
        )
        reply = await _groq_request(
            [
                {"role": "system", "content": _RECAP_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=_RECAP_MAX_TOKENS,
            temperature=0.4,
        )
        reply = _strip_think_blocks(reply)
        if not reply:
            try:
                await thinking.edit_text("◇ ИИ сейчас недоступен — попробуй чуть позже.")
            except Exception:
                pass
            return
        header = (
            f"◆ QUIET MOD · RECAP 👁️\n"
            f"Период: последние {days} дн. · {len(rows)} сообщений из архива\n"
            f"{'─' * 40}\n\n"
        )
        content = (header + reply.strip() + "\n").encode("utf-8")
        try:
            await thinking.delete()
        except Exception:
            pass
        await bot.send_document(
            uid,
            document=BufferedInputFile(content, filename="recap.txt"),
            caption="◇ <b>Рекап готов</b> — кто что писал и о чём договорились",
        )
    except Exception as e:
        log.error(f"recap: {e}")
        try:
            await thinking.edit_text("◇ Не получилось собрать рекап — попробуй ещё раз.")
        except Exception:
            pass
