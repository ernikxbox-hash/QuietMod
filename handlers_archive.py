"""📊 .stats и 🔍 .find — аналитика и ИИ-поиск по твоему архиву.

Обе команды работают ТОЛЬКО в ЛС с ботом: архив принадлежит владельцу,
и никто другой доступа к нему не имеет (конфиденциальность).

.stats — красивая карточка статистики архива: сообщения, дни, чаты,
собеседники, час пик, «ночной волк», медиа, топ слов, самый длинный спич.

.find запрос — смысловой поиск по архиву: бот находит сообщения по
ключевым словам запроса, ИИ выбирает релевантные и отвечает с цитатами
(кто и когда писал). Если ничего не нашлось — вежливо скажет, что такого,
скорее всего, не было, или предложит переформулировать запрос.
"""
import re
from collections import Counter
from datetime import datetime

from aiogram import F
from aiogram.types import Message

import database as db
from core import dp, log
from functions import (
    LINE,
    _clean_ai_latex,
    _groq_request,
    _md_emphasis_to_html,
    _normalize_code_blocks,
    _strip_think_blocks,
)

# Слова, которые не ищем (и не считаем в топ-словах).
_STOP_WORDS = {
    "и", "в", "во", "не", "что", "я", "он", "на", "с", "со", "по", "это", "как",
    "а", "то", "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы",
    "за", "бы", "от", "мы", "о", "мне", "еще", "нет", "есть", "ну", "вот", "только",
    "было", "будет", "меня", "тебе", "себя", "очень", "уже", "из", "или", "до",
    "если", "при", "об", "про", "для", "чтобы", "тоже", "просто", "можно", "надо",
    "щас", "сейчас", "потом", "здесь", "там", "кто", "чего", "тебя", "него", "нее",
    "нам", "вас", "им", "быть", "этот", "этот", "эта", "это", "эти", "тот", "та",
    "когда", "потому", "почему", "опять", "снова", "даже", "был", "была", "были",
}

_FIND_MAX_TOKENS = 1024
_FIND_CONTEXT_LIMIT = 12   # сколько фрагментов отдаём ИИ
_FIND_SNIPPET = 250        # длина фрагмента одного сообщения

_FIND_SYSTEM = (
    "Ты — поисковик по личному архиву переписки пользователя. Тебе дадут "
    "запрос и найденные фрагменты переписки (могут быть удалённые сообщения).\n"
    "Найди ответ на запрос. Если ответ есть — ответь КРАТКО и обязательно "
    "приведи цитату (кто и когда писал). Если в фрагментах нет нужного — "
    "честно скажи, что такого, скорее всего, не было, или что запрос стоит "
    "переформулировать. Не выдумывай. Отвечай по-русски, без воды."
)


def _extract_keywords(text: str) -> list[str]:
    """Значимые слова запроса: нижний регистр, без мусора и стоп-слов."""
    words = re.findall(r"[а-яёa-z0-9]{3,}", (text or "").lower())
    return [w for w in words if w not in _STOP_WORDS][:8]


def _top_words(texts: list[str], n: int = 10) -> list[tuple[str, int]]:
    c: Counter = Counter()
    for t in texts:
        for w in re.findall(r"[а-яёa-z0-9]{3,}", t.lower()):
            if w not in _STOP_WORDS:
                c[w] += 1
    return c.most_common(n)


def _sender_label(row: dict) -> str:
    """Красивый отправитель: один @ + username, или имя. В БД username хранится
    с «@» — убираем лишний, чтобы не было «@@name»."""
    uname = (row.get("username") or "").strip().lstrip("@")
    if uname:
        return f"@{uname}"
    return (row.get("from_name") or "?").strip() or "?"


def _clean_name(name: str, limit: int = 28) -> str:
    """Чистим имя чата/юзера: схлопываем пробелы, режем длину, убираем мусорные символы."""
    s = re.sub(r"\s+", " ", (name or "").strip())
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s or "Личные"


# ── .stats — статистика архива ────────────────────────────────────────
def _render_stats(st: dict) -> str:
    total = st.get("total") or 0
    lines = [
        "◆ <b>СТАТИСТИКА АРХИВА</b>",
        f"<code>{LINE}</code>",
        f"◇ Сообщений в архиве: <b>{total}</b>",
    ]
    first, last = st.get("first"), st.get("last")
    if first and last:
        try:
            d0 = datetime.fromisoformat(first)
            d1 = datetime.fromisoformat(last)
            days = max(1, (d1 - d0).days + 1)
            per_day = total / days
            lines.append(f"◇ В архиве: <b>{days} дн.</b> · в среднем <b>{per_day:g}</b> сообщ./день")
        except Exception:
            pass
    lines.append(f"◇ Медиа: <b>{st.get('media') or 0}</b>")

    if st.get("chats"):
        lines += ["", "◆ <b>ЧАТЫ</b>"]
        for c in st["chats"][:5]:
            lines.append(f"◇ {_clean_name(c['chat'])} — <b>{c['c']}</b>")

    if st.get("senders"):
        lines += ["", "◆ <b>СОБЕСЕДНИКИ</b>"]
        for s in st["senders"][:5]:
            lines.append(f"◇ {_sender_label(s)} — <b>{s['c']}</b>")

    hours = st.get("hours") or {}
    if hours:
        peak = max(hours, key=hours.get)
        lines += ["", "◆ <b>АКТИВНОСТЬ</b>"]
        lines.append(f"◇ Час пик: <b>{peak:02d}:00</b>")
        lines.append(f"◇ Ночной волк (00:00–05:00): <b>{st.get('night') or 0}</b> сообщ.")

    if st.get("media_types"):
        parts = [
            f"{(m['media_type'] or '').replace('◆ ', '', 1)} <b>{m['c']}</b>"
            for m in st["media_types"][:5]
        ]
        lines += ["", "◆ <b>МЕДИА</b>", "◇ " + " · ".join(parts)]

    words = _top_words(st.get("texts") or [])
    if words:
        parts = [f"{w} <b>{c}</b>" for w, c in words[:10]]
        lines += ["", "◆ <b>ТОП СЛОВ</b>", "◇ " + " · ".join(parts)]

    longest = st.get("longest")
    if longest and (longest.get("text") or "").strip():
        t = longest["text"].strip()
        who = _sender_label(longest)
        lines += ["", "◆ <b>САМЫЙ ДЛИННЫЙ СПИЧ</b>", f"◇ <i>{who}:</i> {t[:200]}{'…' if len(t) > 200 else ''}"]

    lines += [
        "",
        f"<code>{LINE}</code>",
        "◇ <code>.find запрос</code> — поиск по архиву",
    ]
    return "\n".join(lines)


@dp.message(F.text.regexp(r"(?i)^\.stats(\s+.*)?$"), F.chat.type == "private")
async def on_stats_cmd(msg: Message):
    if not msg.from_user:
        return
    thinking = await msg.answer("◆ · · ·")
    try:
        st = await db.get_archive_stats(msg.from_user.id)
        if not (st.get("total") or 0):
            try:
                await thinking.edit_text(
                    "◇ В архиве пока пусто.\n"
                    "◇ Подключи бота к чату — и он начнёт сохранять сообщения, "
                    "а статистика появится здесь."
                )
            except Exception:
                pass
            return
        await thinking.edit_text(_render_stats(st))
    except Exception as e:
        log.error(f"stats: {e}")
        try:
            await thinking.edit_text("◇ Не получилось собрать статистику — попробуй ещё раз.")
        except Exception:
            pass


# ── .find — ИИ-поиск по архиву ────────────────────────────────────────
async def _find_reply(uid: int, query: str) -> str:
    """Ищет по архиву и возвращает готовый текст ответа (HTML)."""
    keywords = _extract_keywords(query)
    if not keywords:
        return "◇ Слишком короткий запрос — добавь пару слов, так искать легче."
    rows = await db.search_messages_keywords(uid, keywords)
    scored = []
    for r in rows:
        t = (r.get("text") or "").lower()
        hits = sum(1 for kw in keywords if kw in t)
        if hits:
            scored.append((hits, r))
    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    top = scored[:_FIND_CONTEXT_LIMIT]
    if not top:
        return (
            "◇ <b>Не нашёл</b> в архиве ничего похожего.\n"
            "◇ Скорее всего, такого не было — или попробуй переформулировать запрос."
        )
    context = "\n".join(
        f"[{_clean_name(r.get('chat') or '?')}] {_sender_label(r)} ({r.get('date')}): "
        f"{(r.get('text') or '').strip()[:_FIND_SNIPPET]}"
        for _, r in top
    )
    prompt = (
        f"Пользователь ищет в своём архиве Telegram-переписки: «{query}»\n"
        f"Найдены фрагменты (могут быть удалённые сообщения):\n\n{context}\n\n"
        "Дай ответ по правилам из системного промпта."
    )
    reply = await _groq_request(
        [
            {"role": "system", "content": _FIND_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        max_tokens=_FIND_MAX_TOKENS,
        temperature=0.3,
    )
    reply = _strip_think_blocks(reply)
    if not reply:
        return "◇ ИИ сейчас недоступен — попробуй чуть позже."
    return _md_emphasis_to_html(_clean_ai_latex(_normalize_code_blocks(reply)))


@dp.message(F.text.regexp(r"(?i)^\.find\s+(.+)$"), F.chat.type == "private")
async def on_find_cmd(msg: Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    raw = msg.text or ""
    query = raw[raw.index(" ") + 1:].strip()
    thinking = await msg.answer("◆ · · ·")
    try:
        text = await _find_reply(uid, query)
        try:
            await thinking.edit_text(
                f"◇ <b>ПОИСК ПО АРХИВУ</b>\n<code>{LINE}</code>\n\n{text}"
            )
        except Exception:
            await msg.answer(
                f"◇ <b>ПОИСК ПО АРХИВУ</b>\n<code>{LINE}</code>\n\n{text}"
            )
    except Exception as e:
        log.error(f"find: {e}")
        try:
            await thinking.edit_text("◇ Не получилось выполнить поиск — попробуй ещё раз.")
        except Exception:
            pass
