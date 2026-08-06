"""✨ .шрифт — стили текста: капс, транслит, пацан, хак, готика, мат и т.д.

Форматы:
    .шрифт              — инструкция + список стилей
    .шрифт стиль текст  — применить стиль
    .шрифт стиль        — применить к тексту сообщения, на которое ответил

Работает в ЛС с ботом, бизнес-чатах, группах и каналах. В бизнес-чате
командное сообщение редактируется в стилизованный текст (как .bold/.italic),
в группах и ЛС — отправляется новое сообщение.
"""
from html import escape as html_escape
from typing import Callable, Optional

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.types import Message

from business_api import _business_edit_message, _get_owner_id_cached
from core import BOT_USERNAME, log
from functions import LINE

_MAX_INPUT = 800     # максимум символов исходного текста
_MAX_OUTPUT = 3500   # безопасный максимум стилизованного вывода


# ── Преобразования текста ─────────────────────────────────────────────
_TRANSLIT_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "'", "ы": "y", "ь": "'", "э": "e", "ю": "yu", "я": "ya",
}
# Псевдолатиница «пацанский»: русское слово буквами, похожими на латинские (nPuBeT)
_FAUX_MAP = {
    "а": "a", "б": "6", "в": "B", "г": "r", "д": "g", "е": "e", "ё": "e",
    "ж": "X", "з": "3", "и": "u", "й": "u", "к": "K", "л": "J", "м": "M",
    "н": "H", "о": "o", "п": "n", "р": "p", "с": "C", "т": "T", "у": "y",
    "ф": "Q", "х": "X", "ц": "U", "ч": "4", "ш": "W", "щ": "W", "ъ": "b",
    "ы": "b", "ь": "b", "э": "3", "ю": "IO", "я": "R",
}
# Леетспик (пр1в3т)
_LEET_MAP = {
    "а": "4", "б": "6", "в": "8", "г": "9", "е": "3", "з": "3", "и": "1",
    "о": "0", "с": "5", "т": "7", "ч": "4",
}


def _translit_lower(text: str) -> str:
    """Кириллица → латиница (в нижний регистр)."""
    return "".join(_TRANSLIT_MAP.get(c, c) for c in (text or "").lower())


def _caps(t: str) -> str:
    return t.upper()


def _lower(t: str) -> str:
    return t.lower()


def _wave(t: str) -> str:
    return "".join(c.upper() if i % 2 == 1 else c.lower() for i, c in enumerate(t))


def _mirror(t: str) -> str:
    return t[::-1]


def _faux(t: str) -> str:
    return "".join(_FAUX_MAP.get(c, c) for c in t)


def _leet(t: str) -> str:
    return "".join(_LEET_MAP.get(c, c) for c in t)


def _space(t: str) -> str:
    return " ".join(t)


def _dots(t: str) -> str:
    return "·".join(t)


def _brackets(t: str) -> str:
    return "".join(f"[{c}]" for c in t)


def _angles(t: str) -> str:
    return "".join(f"‹{c}›" for c in t)


def _combining(mark: str) -> Callable[[str], str]:
    """Стиль с комбинирующим символом после каждой буквы (работает и с кириллицей)."""
    def fn(t: str) -> str:
        return "".join(c + mark if c.isalnum() else c for c in t)
    return fn


_overline  = _combining("\u0305")  # п̅р̅и̅в̅е̅т̅
_underline = _combining("\u0332")  # п̲р̲и̲в̲е̲т̲
_strike    = _combining("\u0336")  # п̶р̶и̶в̶е̶т̶
_rock      = _combining("\u0308")  # п̈р̈ӥв̈ёт̈
_dot_above = _combining("\u0307")  # п̇р̇и̇в̇е̇т̇
_circle    = _combining("\u20dd")  # п⃝р⃝и⃝в⃝е⃝т⃝


def _math_style(start_cp: int) -> Callable[[str], str]:
    """Юникод-математический стиль по смещению (прописные и строчные)."""
    def fn(t: str) -> str:
        out = []
        for c in t:
            if "a" <= c <= "z":
                out.append(chr(start_cp + (ord(c) - ord("a"))))
            elif "A" <= c <= "Z":
                out.append(chr(start_cp + 26 + (ord(c) - ord("A"))))
            else:
                out.append(c)
        return "".join(out)
    return fn


# Прописные скрипта имеют «дыры» в Юникоде (ℬ ℰ ℱ ℋ ℐ ℒ ℳ ℛ вынесены в
# Letterlike Symbols) — поэтому строчные задаём явно, заглавные — через строчные.
_SCRIPT_EXC = {"e": "\u212f", "g": "\u210a", "o": "\u2134"}


def _script(t: str) -> str:
    out = []
    for c in t:
        if c in _SCRIPT_EXC:
            out.append(_SCRIPT_EXC[c])
        elif "a" <= c <= "z":
            out.append(chr(0x1D4B6 + (ord(c) - ord("a"))))
        elif "A" <= c <= "Z":
            out.append(chr(0x1D4B6 + (ord(c.lower()) - ord("a"))))
        else:
            out.append(c)
    return "".join(out)


def _unicode_style(fn: Callable[[str], str]) -> Callable[[str], str]:
    """Обёртка для юникод-стилей: сначала транслит кириллицы, потом стиль."""
    def wrapped(t: str) -> str:
        return fn(_translit_lower(t))
    return wrapped


# ── Реестр стилей ──────────────────────────────────────────────────────
# key: канонический ключ; ru: подпись в списке; aliases: как можно ввести;
# fn: функция-преобразователь (получает исходный текст).
SHRIFT_STYLES: dict[str, dict] = {
    "caps":      {"ru": "капс",      "aliases": ("капс", "caps", "заглавные", "кричать"),      "fn": _caps},
    "lower":     {"ru": "низ",       "aliases": ("низ", "lower", "мелкие", "строчные"),        "fn": _lower},
    "wave":      {"ru": "волна",     "aliases": ("волна", "wave", "капсик"),                   "fn": _wave},
    "mirror":    {"ru": "зеркало",   "aliases": ("зеркало", "mirror", "реверс", "наоборот"),   "fn": _mirror},
    "translit":  {"ru": "транслит",  "aliases": ("транслит", "translit", "латиница"),          "fn": _translit_lower},
    "faux":      {"ru": "пацан",     "aliases": ("пацан", "пацанский", "faux", "npu"),         "fn": _faux},
    "leet":      {"ru": "хак",       "aliases": ("хак", "хакер", "leet", "l33t", "леет"),       "fn": _leet},
    "space":     {"ru": "разрядка",  "aliases": ("разрядка", "space", "пробел", "разряд"),     "fn": _space},
    "dots":      {"ru": "точки",     "aliases": ("точки", "dots"),                              "fn": _dots},
    "brackets":  {"ru": "скобки",    "aliases": ("скобки", "brackets", "скобка"),              "fn": _brackets},
    "angles":    {"ru": "углы",      "aliases": ("углы", "angles", "угол"),                    "fn": _angles},
    "overline":  {"ru": "черта",     "aliases": ("черта", "overline", "сверху", "над"),        "fn": _overline},
    "underline": {"ru": "подчёрк",   "aliases": ("подчёрк", "подчерк", "underline", "снизу"),  "fn": _underline},
    "strike":    {"ru": "зачёрк",    "aliases": ("зачёрк", "зачерк", "strike", "крест"),       "fn": _strike},
    "rock":      {"ru": "рок",       "aliases": ("рок", "rock", "умляут", "метал"),            "fn": _rock},
    "dot":       {"ru": "точка",     "aliases": ("точка", "dot", "надточка"),                  "fn": _dot_above},
    "circle":    {"ru": "круг",      "aliases": ("круг", "circle", "кружок"),                  "fn": _circle},
    "math":      {"ru": "мат",       "aliases": ("мат", "math", "дабл", "двойной"),            "fn": _unicode_style(_math_style(0x1D552))},
    "fraktur":   {"ru": "готика",    "aliases": ("готика", "готик", "fraktur", "древний"),     "fn": _unicode_style(_math_style(0x1D51E))},
    "script":    {"ru": "скрипт",    "aliases": ("скрипт", "script", "прописной"),             "fn": _unicode_style(_script)},
    "italic":    {"ru": "курсив",    "aliases": ("курсив", "italic", "наклонный"),             "fn": _unicode_style(_math_style(0x1D44E))},
    "mono":      {"ru": "моно",      "aliases": ("моно", "mono", "машинопись"),                "fn": _unicode_style(_math_style(0x1D656))},
    "sans":      {"ru": "sans",      "aliases": ("sans", "санс", "чистый"),                    "fn": _unicode_style(_math_style(0x1D586))},
    "bold":      {"ru": "жирный",    "aliases": ("жирный", "bold", "толстый"),                 "fn": _unicode_style(_math_style(0x1D41A))},
}

_STYLE_GROUPS: list[tuple[str, list[str]]] = [
    ("Регистр",     ["caps", "lower", "wave", "mirror"]),
    ("Раскладка",   ["translit", "faux", "leet", "space", "dots", "brackets", "angles"]),
    ("Диакритика",  ["overline", "underline", "strike", "rock", "dot", "circle"]),
    ("Юникод",      ["math", "fraktur", "script", "italic", "mono", "sans", "bold"]),
]


def _styles_line() -> str:
    lines = []
    for label, keys in _STYLE_GROUPS:
        names = " · ".join(SHRIFT_STYLES[k]["ru"] for k in keys)
        lines.append(f"◇ <b>{label}:</b> {names}")
    return "\n".join(lines)


_SHRIFT_HELP = (
    "◆ <b>ШРИФТ</b> — стили текста\n"
    f"<code>{LINE}</code>\n\n"
    "◇ Применить:\n"
    "   <code>.шрифт капс привет</code>\n"
    "   <code>.шрифт готика привет</code>\n"
    "◇ Или ответь на сообщение:\n"
    "   <code>.шрифт капс</code>\n\n"
    f"{_styles_line()}\n\n"
    f"<code>{LINE}</code>\n"
    f"— 👁️ @{BOT_USERNAME}"
)

_SHRIFT_UNKNOWN = (
    "◇ <b>Такого стиля нет.</b>\n\n"
    f"{_styles_line()}\n\n"
    "◇ Все стили: <code>.шрифт</code>"
)

_SHRIFT_NO_TEXT = (
    "◇ Добавь текст или ответь на сообщение:\n"
    "   <code>.шрифт капс привет</code>"
)


def _shrift_parse(rest: str) -> tuple[Optional[str], str]:
    """('.шрифт капс привет' → 'caps','привет'). Стиль — первое слово."""
    parts = (rest or "").split(maxsplit=1)
    if not parts:
        return None, ""
    word = parts[0].strip().lower()
    text = parts[1].strip() if len(parts) > 1 else ""
    for key, st in SHRIFT_STYLES.items():
        if word == key or word in st["aliases"]:
            return key, text
    return None, rest


def _shrift_apply(key: str, text: str, reply: Optional[Message]) -> Optional[str]:
    """Применяет стиль; None — нет текста (в аргументе или в ответе).

    Возвращает готовую строку (экранированную под HTML).
    """
    if not text and reply:
        text = reply.text or reply.caption or ""
    if not text:
        return None
    styled = SHRIFT_STYLES[key]["fn"](text[:_MAX_INPUT])
    if len(styled) > _MAX_OUTPUT:
        styled = styled[:_MAX_OUTPUT] + "…"
    return html_escape(styled)


# ── Бизнес-чаты: командное сообщение редактируется в стилизованный текст ──
@dp.business_message(F.text.regexp(r"(?is)^\.шрифт(?:\s+.*)?$"))
async def on_shrift_inline(msg: Message):
    if not msg.business_connection_id:
        return
    owner_id = await _get_owner_id_cached(msg.business_connection_id, ".шрифт")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    raw = (msg.text or msg.caption or "").strip()
    rest = raw[len(".шрифт"):].strip() if raw.lower().startswith(".шрифт") else ""
    if not rest:
        await _business_edit_message(
            msg.business_connection_id, msg.chat.id, msg.message_id, _SHRIFT_HELP
        )
        return
    key, text = _shrift_parse(rest)
    if key is None:
        await _business_edit_message(
            msg.business_connection_id, msg.chat.id, msg.message_id, _SHRIFT_UNKNOWN
        )
        return
    styled = _shrift_apply(key, text, msg.reply_to_message)
    if styled is None:
        await _business_edit_message(
            msg.business_connection_id, msg.chat.id, msg.message_id, _SHRIFT_NO_TEXT
        )
        return
    ok = await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id, styled
    )
    log.info(f"✨ .шрифт {key} business chat={msg.chat.id} ok={ok}")


# ── Группы / супергруппы / каналы ──────────────────────────────────────
@dp.message(F.text.regexp(r"(?is)^\.шрифт(?:\s+.*)?$"), F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_shrift_group(msg: Message):
    if not msg.from_user:
        return
    raw = (msg.text or "").strip()
    rest = raw[len(".шрифт"):].strip() if raw.lower().startswith(".шрифт") else ""
    if not rest:
        await msg.answer(_SHRIFT_HELP)
        return
    key, text = _shrift_parse(rest)
    if key is None:
        await msg.answer(_SHRIFT_UNKNOWN)
        return
    styled = _shrift_apply(key, text, msg.reply_to_message)
    if styled is None:
        await msg.answer(_SHRIFT_NO_TEXT)
        return
    try:
        await msg.delete()
    except Exception:
        pass
    await msg.answer(styled)
    log.info(f"✨ .шрифт {key} group chat={msg.chat.id} user={msg.from_user.id}")


# ── ЛС с ботом ─────────────────────────────────────────────────────────
@dp.message(StateFilter("*"), F.text.regexp(r"(?is)^\.шрифт(?:\s+.*)?$"), F.chat.type == "private")
async def on_shrift_dm(msg: Message):
    if not msg.from_user:
        return
    raw = (msg.text or "").strip()
    rest = raw[len(".шрифт"):].strip() if raw.lower().startswith(".шрифт") else ""
    if not rest:
        await msg.answer(_SHRIFT_HELP)
        return
    key, text = _shrift_parse(rest)
    if key is None:
        await msg.answer(_SHRIFT_UNKNOWN)
        return
    styled = _shrift_apply(key, text, msg.reply_to_message)
    if styled is None:
        await msg.answer(_SHRIFT_NO_TEXT)
        return
    await msg.answer(styled)
    log.info(f"✨ .шрифт {key} dm user={msg.from_user.id}")
