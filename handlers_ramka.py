"""🖼 .ramka — золотая орнаментальная рамка на фото.

Три режима:
1. ЛС с ботом: .ramka → бот просит фото → накладывает рамку и возвращает.
   (Если сразу ответить на фото и написать .ramka — рамка наденется сразу.)
2. Бизнес-чат: ответь на фото и напиши .ramka — бот вернёт фото в рамке.
3. Группа/канал: ответь на фото и напиши .ramka — фото в рамке в чат.

Рамка рисуется кодом (Pillow) под размер фото: градиентное золотое тело,
углублённый кант, розетки по углам, картуши по центру, жемчужная полоса
по периметру отверстия и мягкая внутренняя тень. Центр рамки прозрачный —
фото видно целиком, без обрезки.
"""
import asyncio
from io import BytesIO
from typing import Optional

import aiohttp
from aiogram import F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
)
from PIL import Image, ImageDraw, ImageFilter, ImageOps

import database as db
from business_api import (
    _business_delete_message_ex,
    _business_edit_message,
    _get_owner_id_cached,
)
from core import BOT_TOKEN, BOT_USERNAME, S, bot, dp, get_http, log
from functions import LINE


# ── Рисование рамки (Pillow; supersampling для гладких краёв) ──────────
def _ramka_gradient(w: int, h: int, stops: list) -> Image.Image:
    """Вертикальный градиент по стопам (позиция 0..1, RGB-цвет)."""
    g = Image.new("RGB", (1, h))
    px = g.load()
    for y in range(h):
        t = y / max(1, h - 1)
        for i in range(len(stops) - 1):
            p0, c0 = stops[i]
            p1, c1 = stops[i + 1]
            if p0 <= t <= p1:
                k = (t - p0) / max(1e-9, p1 - p0)
                px[0, y] = tuple(int(c0[j] + (c1[j] - c0[j]) * k) for j in range(3))
                break
    return g.resize((w, h), Image.LANCZOS).convert("RGBA")


def _ramka_bead(d, cx: int, cy: int, r: int) -> None:
    """Жемчужина: тело, нижняя тень, блик (3D-эффект)."""
    if r < 1:
        return
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(246, 224, 158, 255))
    d.arc([cx - r, cy - r, cx + r, cy + r], 180, 360,
          fill=(126, 86, 24, 255), width=max(1, int(r * 0.45)))
    hr = max(1, int(r * 0.35))
    hx = cx - r + int(r * 0.18)
    hy = cy - r + int(r * 0.12)
    d.ellipse([hx, hy, hx + hr * 2, hy + hr * 2], fill=(255, 252, 232, 230))


def _ramka_rosette(d, cx: int, cy: int, R: int, ss: int) -> None:
    """Розетка-орнамент: кольцо лепестков, средний круг, клинья, центр."""
    if R < 4:
        return
    d.ellipse([cx - R, cy - R, cx + R, cy + R],
              fill=(148, 102, 32, 255), outline=(104, 70, 16, 255), width=ss)
    for k in range(10):
        a0 = k * 36
        d.arc([cx - R, cy - R, cx + R, cy + R], a0, a0 + 16,
              fill=(216, 178, 86, 255), width=max(1, int(R * 0.28)))
    Rm = int(R * 0.60)
    d.ellipse([cx - Rm, cy - Rm, cx + Rm, cy + Rm],
              fill=(240, 210, 120, 255), outline=(168, 120, 36, 255), width=ss)
    Rv = int(R * 0.40)
    for k in range(6):
        a0 = k * 60 + 24
        d.pieslice([cx - Rv, cy - Rv, cx + Rv, cy + Rv], a0, a0 + 42,
                   fill=(198, 152, 58, 255))
    Rc = max(1, int(R * 0.15))
    d.ellipse([cx - Rc, cy - Rc, cx + Rc, cy + Rc], fill=(255, 244, 200, 255))
    Rb = max(1, int(R * 0.08))
    d.ellipse([cx - Rc + int(R * 0.10), cy - Rc - int(R * 0.22),
               cx - Rc + int(R * 0.10) + Rb * 2, cy - Rc - int(R * 0.22) + Rb * 2],
              fill=(255, 255, 236, 210))


def _ramka_crest(d, cx: int, cy: int, R: int, ss: int) -> None:
    """Картуш (ромб + эллипс + бусины) в центре верхней/нижней стороны."""
    if R < 4:
        return
    d.ellipse([cx - R, cy - int(R * 0.62), cx + R, cy + int(R * 0.62)],
              fill=(226, 192, 100, 255), outline=(138, 94, 24, 255), width=ss)
    d.ellipse([cx - int(R * 0.52), cy - int(R * 0.34),
               cx + int(R * 0.52), cy + int(R * 0.34)],
              fill=(250, 226, 152, 255))
    for sgn in (-1, 1):
        d.polygon(
            [(cx, cy + int(R * 0.90) * sgn),
             (cx - int(R * 0.34), cy + int(R * 0.52) * sgn),
             (cx + int(R * 0.34), cy + int(R * 0.52) * sgn)],
            fill=(196, 150, 58, 255),
        )
        d.ellipse([cx - int(R * 0.12), cy + int(R * 0.82) * sgn,
                   cx + int(R * 0.12), cy + int(R * 1.02) * sgn],
                  fill=(255, 240, 190, 255))


def _ramka_draw(pw: int, ph: int) -> Image.Image:
    """Орнаментальная золотая рамка под размер фото (pw×ph), центр прозрачный."""
    SS = 2  # supersampling: рисуем в 2 раза крупнее и сглаживаем
    W, H = pw * SS, ph * SS
    b = max(26, int(min(pw, ph) * 0.115)) * SS
    b = min(b, min(W, H) // 2 - 8 * SS)
    if b <= 0:
        b = max(2, min(W, H) // 3)
    r_out = max(2, int(b * 0.38))

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # 1. Тело рамки — золотой градиент с мягким скруглением
    body_grad = _ramka_gradient(W, H, [
        (0.00, (243, 219, 136)),
        (0.16, (228, 192, 100)),
        (0.52, (199, 154, 60)),
        (0.78, (178, 130, 46)),
        (1.00, (150, 104, 34)),
    ])
    body_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(body_mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=r_out, fill=255)
    img.alpha_composite(Image.composite(
        body_grad, Image.new("RGBA", (W, H), (0, 0, 0, 0)), body_mask
    ))

    # 2. Внутренний кант (углублённый канал) — темнее
    i1 = int(b * 0.16)
    chan_grad = _ramka_gradient(W, H, [
        (0.00, (204, 166, 74)),
        (1.00, (124, 84, 22)),
    ])
    chan_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(chan_mask).rounded_rectangle(
        [i1, i1, W - 1 - i1, H - 1 - i1], radius=max(2, r_out - i1), fill=255
    )
    img.alpha_composite(Image.composite(
        chan_grad, Image.new("RGBA", (W, H), (0, 0, 0, 0)), chan_mask
    ))

    # 3. Орнаменты — отдельный слой (для полупрозрачности нужен alpha_composite)
    orn = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(orn)

    # свет сверху: светлая фаска у верхней кромки тела, тень у нижней
    d.rounded_rectangle([SS, SS, W - 1 - SS, int(b * 0.34)], radius=r_out,
                        fill=(255, 246, 208, 70))
    d.rounded_rectangle([SS, H - 1 - int(b * 0.26), W - 1 - SS, H - 1 - SS], radius=r_out,
                        fill=(58, 36, 6, 90))

    # освещение канала: тень на верхней стенке, блик на нижней
    cb = [i1, i1, W - 1 - i1, H - 1 - i1]
    d.arc(cb, 0, 180, fill=(88, 60, 14, 200), width=max(2, SS * 2))
    d.arc(cb, 180, 360, fill=(255, 238, 190, 220), width=max(2, SS * 2))

    # розетки по углам
    cc = int(b * 0.55)
    for cx, cy in ((cc, cc), (W - cc, cc), (cc, H - cc), (W - cc, H - cc)):
        _ramka_rosette(d, cx, cy, int(b * 0.40), SS)

    # картуши сверху и снизу по центру
    _ramka_crest(d, W // 2, int(b * 0.58), int(b * 0.30), SS)
    _ramka_crest(d, W // 2, H - int(b * 0.58), int(b * 0.30), SS)

    # розетки по центру боковин
    _ramka_rosette(d, cc, H // 2, int(b * 0.28), SS)
    _ramka_rosette(d, W - cc, H // 2, int(b * 0.28), SS)

    img.alpha_composite(orn)

    # 4. Жемчужная полоса по периметру отверстия
    ox0, oy0, ox1, oy1 = b, b, W - b, H - b
    rb = max(2, int(b * 0.075))
    step = max(3, int(rb * 2.4))
    beads = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(beads)
    x = ox0
    while x <= ox1:
        _ramka_bead(bd, x, oy0, rb)
        _ramka_bead(bd, x, oy1, rb)
        x += step
    y = oy0
    while y <= oy1:
        _ramka_bead(bd, ox0, y, rb)
        _ramka_bead(bd, ox1, y, rb)
        y += step
    big = int(rb * 1.7)
    for bx, by in ((ox0, oy0), (ox1, oy0), (ox0, oy1), (ox1, oy1)):
        _ramka_bead(bd, bx, by, big)
    img.alpha_composite(beads)

    # 5. Мягкая тень внутри отверстия — фото выглядит вставленным в рамку
    sh_w = max(2, int(b * 0.13))
    ring = Image.new("L", (W, H), 0)
    rd = ImageDraw.Draw(ring)
    rd.rectangle([ox0, oy0, ox1, oy1], fill=255)
    rd.rectangle([ox0 + sh_w, oy0 + sh_w, ox1 - sh_w, oy1 - sh_w], fill=0)
    ring = ring.filter(ImageFilter.GaussianBlur(max(1, int(sh_w * 0.55))))
    img.alpha_composite(Image.composite(
        Image.new("RGBA", (W, H), (0, 0, 0, 175)),
        Image.new("RGBA", (W, H), (0, 0, 0, 0)),
        ring,
    ))

    # 6. Чёткие кромки: тёмная по отверстию + контур тела
    trim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    td = ImageDraw.Draw(trim)
    td.rectangle([ox0, oy0, ox1, oy1], outline=(84, 56, 10, 255), width=max(2, SS * 2))
    td.rounded_rectangle([0, 0, W - 1, H - 1], radius=r_out,
                         outline=(72, 46, 8, 255), width=max(2, SS * 2))
    img.alpha_composite(trim)

    return img.resize((pw, ph), Image.LANCZOS)


def _ramka_render(data: bytes) -> bytes:
    """Синхронно (в потоке): фото → фото в золотой рамке, JPEG-байты."""
    im = Image.open(BytesIO(data))
    im = ImageOps.exif_transpose(im).convert("RGB")
    if im.width * im.height > 4_000_000:
        im.thumbnail((1920, 1920), Image.LANCZOS)
    w, h = im.size
    frame = _ramka_draw(w, h)
    out = im.convert("RGBA")
    out.alpha_composite(frame)
    buf = BytesIO()
    out.convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue()


async def _ramka_process_photo(photo: PhotoSize) -> bytes:
    """Скачивает фото и возвращает фото в рамке (обработка в фоновом потоке)."""
    file = await bot.get_file(photo.file_id)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
    session = get_http()
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"download photo: status {resp.status}")
        data = await resp.read()
    return await asyncio.to_thread(_ramka_render, data)


async def _ramka_run(photo: PhotoSize, chat_id: int,
                     business_connection_id: Optional[str] = None) -> bool:
    """Накладывает рамку и отправляет результат в чат. True — успех."""
    try:
        png = await _ramka_process_photo(photo)
    except Exception as e:
        log.error(f"ramka process: {e}")
        return False
    try:
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(png, filename="ramka.jpg"),
            business_connection_id=business_connection_id,
        )
        return True
    except Exception as e:
        log.error(f"ramka send: {e}")
        return False


async def _ramka_cleanup(thinking: Optional[Message]) -> None:
    if thinking is None:
        return
    try:
        await thinking.delete()
    except Exception:
        pass


async def _ramka_status(photo: PhotoSize, chat_id: int, send_fn,
                        business_connection_id: Optional[str] = None) -> bool:
    """Статус-сообщение → обработка фото → уборка. False при ошибке (с ответом)."""
    thinking = await send_fn("🖼 Делаю рамку…")
    ok = await _ramka_run(photo, chat_id, business_connection_id=business_connection_id)
    await _ramka_cleanup(thinking)
    if not ok:
        await send_fn("😔 Не получилось наложить рамку — попробуй другое фото.")
    return ok


def _ramka_reply_photo(msg: Message) -> Optional[PhotoSize]:
    r = msg.reply_to_message
    if r and r.photo:
        return r.photo[-1]
    return None


_RAMKA_HINT = (
    f"🖼 <b>.ramka</b> — ответь на чьё-то <b>фото</b> и напиши <code>.ramka</code>,\n"
    f"◇ я надену на него золотую рамку.\n\n"
    f"— 👁️ @{BOT_USERNAME}"
)


# ── ЛС с ботом: .ramka → просим фото ──────────────────────────────────
@dp.message(StateFilter("*"), F.text.regexp(r"(?i)^\.ramka(\s+.*)?$"), F.chat.type == "private")
async def on_ramka_dm(msg: Message, state: FSMContext):
    if not msg.from_user:
        return
    # сразу ответили на фото — применяем без лишних шагов
    reply_photo = _ramka_reply_photo(msg)
    if reply_photo:
        await state.clear()
        await _ramka_status(reply_photo, msg.chat.id, msg.answer)
        return
    await state.set_state(S.ramka)
    await msg.answer(
        f"🖼 <b>ЗОЛОТАЯ РАМКА</b>\n{LINE}\n\n"
        "◇ Пришли фото — надену на него рамку.\n"
        "◇ Или ответь на чьё-то фото и напиши <code>.ramka</code> —\n"
        "   так работает и в чатах.\n\n"
        f"— 👁️ @{BOT_USERNAME}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отмена", callback_data="ramka_cancel")]
        ]),
    )


@dp.message(S.ramka)
async def on_ramka_photo_input(msg: Message, state: FSMContext):
    if not msg.photo:
        await msg.answer("◇ Пришли именно <b>фото</b> — рамку умею надевать только на картинки.")
        return
    await state.clear()
    await _ramka_status(msg.photo[-1], msg.chat.id, msg.answer)


@dp.callback_query(F.data == "ramka_cancel")
async def cb_ramka_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("✕ Отменено", show_alert=False)
    await _ramka_cleanup(call.message)


# ── Бизнес-чат: ответь на фото + .ramka ───────────────────────────────
@dp.business_message(F.text.regexp(r"(?i)^\.ramka(\s+.*)?$"))
async def on_ramka_business(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return
    owner_id = await _get_owner_id_cached(conn_id, ".ramka")
    if owner_id is None:
        return
    if not msg.from_user or msg.from_user.id != owner_id:
        return
    photo = _ramka_reply_photo(msg)
    if not photo:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id,
            f"🖼 <b>.ramka</b> — ответь на чьё-то <b>фото</b> и напиши <code>.ramka</code>.\n\n"
            f"— 👁️ @{BOT_USERNAME}",
        )
        return
    ok = await _business_edit_message(conn_id, msg.chat.id, msg.message_id, "🖼 Делаю рамку…")
    if not ok:
        return
    if await _ramka_run(photo, msg.chat.id, business_connection_id=conn_id):
        try:
            await _business_delete_message_ex(conn_id, msg.message_id)
        except Exception:
            pass
    else:
        await _business_edit_message(
            conn_id, msg.chat.id, msg.message_id,
            "😔 Не получилось наложить рамку — попробуй другое фото.",
        )


# ── Группа / канал: ответь на фото + .ramka ───────────────────────────
@dp.message(F.text.regexp(r"(?i)^\.ramka(\s+.*)?$"),
            F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_ramka_group(msg: Message):
    if not msg.from_user:
        return
    await db.upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    await db.add_bot_chat(msg.chat.id, msg.chat.title or "", msg.chat.type)
    photo = _ramka_reply_photo(msg)
    if not photo:
        await msg.reply(_RAMKA_HINT)
        return
    await _ramka_status(photo, msg.chat.id, msg.reply)
