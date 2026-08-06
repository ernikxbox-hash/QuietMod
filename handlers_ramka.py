"""🖼 .ramka — золотая орнаментальная рамка на фото.

Три режима:
1. ЛС с ботом: .ramka → бот просит фото → накладывает рамку и возвращает.
   (Если сразу ответить на фото и написать .ramka — рамка наденется сразу.)
2. Бизнес-чат: ответь на фото и напиши .ramka — бот вернёт фото в рамке.
3. Группа/канал: ответь на фото и напиши .ramka — фото в рамке в чат.

Рамка по умолчанию рисуется кодом (Pillow) под размер фото в барочном стиле:
античное золото, резные флейты, волюты по углам, вееры, свитки, жемчужная кайма.
Но если в env задан RAMKA_URL — ссылка на PNG-рамку с прозрачным отверстием
(например, raw-файл на GitHub), бот качает её (кэш 10 минут) и накладывает на
фото: картинка заливает отверстие целиком (cover-fit, края могут подрезаться).
Ссылки нет или скачать не удалось → фолбэк на рисованную рамку.
"""
import asyncio
import time
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
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

import database as db
from business_api import (
    _business_delete_message_ex,
    _business_edit_message,
    _get_owner_id_cached,
)
from core import BOT_TOKEN, BOT_USERNAME, RAMKA_URL, S, bot, dp, get_http, log
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


def _ramka_flute_h(d, x0: int, x1: int, y0: int, count: int, step: int, w: int = 2) -> None:
    """Флейты — резные бороздки вдоль горизонтального молдинга."""
    y = y0
    for _ in range(count):
        d.line([x0, y, x1, y], fill=(150, 104, 34, 255), width=w)
        d.line([x0, y + w, x1, y + w], fill=(248, 226, 152, 255), width=w)
        y += step


def _ramka_flute_v(d, y0: int, y1: int, x0: int, count: int, step: int, w: int = 2) -> None:
    """Флейты — резные бороздки вдоль вертикального молдинга."""
    x = x0
    for _ in range(count):
        d.line([x, y0, x, y1], fill=(150, 104, 34, 255), width=w)
        d.line([x + w, y0, x + w, y1], fill=(248, 226, 152, 255), width=w)
        x += step


def _ramka_baroque_corner(d, cx: int, cy: int, R: int, ss: int, sx: int = 1, sy: int = 1) -> None:
    """Барокко-угол: резная выемка + витая спираль-волюта + лепестки."""
    if R < 8:
        return
    if sx == 1 and sy == 1:
        a0 = 0
    elif sx == -1 and sy == 1:
        a0 = 90
    elif sx == -1 and sy == -1:
        a0 = 180
    else:
        a0 = 270
    # тёмная резная выемка (четверть, смотрит внутрь рамки)
    d.pieslice([cx - R, cy - R, cx + R, cy + R], a0, a0 + 90,
               fill=(96, 62, 16, 255), outline=(70, 46, 8, 255))
    # витая волюта — дуги уменьшающегося радиуса (спираль внутрь)
    r = R
    a = a0 + 90
    for _ in range(6):
        if r < 4:
            break
        d.arc([cx - r, cy - r, cx + r, cy + r], a, a + 90,
              fill=(226, 192, 100, 255), width=max(2, int(r * 0.16)))
        d.arc([cx - r, cy - r, cx + r, cy + r], a + 5, a + 95,
              fill=(120, 80, 20, 255), width=max(2, int(r * 0.08)))
        r = int(r * 0.74)
        a += 90
    # лепестки-листья вокруг волюты
    for k in range(3):
        la = a0 + 15 + k * 25
        d.pieslice([cx - R, cy - R, cx + R, cy + R], la, la + 18,
                   fill=(216, 178, 86, 255))
    # жемчужина в центре
    _ramka_bead(d, cx + sx * int(R * 0.30), cy + sy * int(R * 0.30), max(2, int(R * 0.12)))


def _ramka_shell_crest(d, cx: int, cy: int, R: int, ss: int, flip: int = 1) -> None:
    """Раковина-веер по центру верха (flip=1) или низа (flip=-1)."""
    if R < 6:
        return
    a_c = 270 if flip > 0 else 90  # центр веера: верх или низ
    for k in range(5):
        a0 = a_c - 40 + k * 20
        d.arc([cx - R, cy - R, cx + R, cy + R], a0, a0 + 14,
              fill=(226, 192, 100, 255), width=max(2, int(R * 0.12)))
        d.arc([cx - R, cy - R, cx + R, cy + R], a0 + 3, a0 + 17,
              fill=(120, 80, 20, 255), width=max(1, int(R * 0.05)))
    # центральный овал-щит, вытянутый к краю рамки
    if flip > 0:
        oy0, oy1 = cy - int(R * 0.8), cy + int(R * 0.15)
    else:
        oy0, oy1 = cy - int(R * 0.15), cy + int(R * 0.8)
    d.ellipse([cx - int(R * 0.35), oy0, cx + int(R * 0.35), oy1],
              fill=(248, 226, 152, 255), outline=(120, 82, 22, 255), width=ss)
    d.ellipse([cx - int(R * 0.20), oy0 + int(R * 0.2), cx + int(R * 0.20), oy1 - int(R * 0.2)],
              fill=(255, 240, 190, 255))
    _ramka_bead(d, cx, cy, max(2, int(R * 0.14)))


def _ramka_side_cartouche(d, cx: int, cy: int, R: int, ss: int, fx: int = -1) -> None:
    """Свиток-картуш по центру боковины (fx=-1 слева, 1 справа)."""
    if R < 5:
        return
    # тёмная подложка-овал (выпуклая к краю рамки)
    d.ellipse([cx - int(R * 0.5), cy - R, cx + int(R * 0.5), cy + R],
              fill=(96, 62, 16, 255), outline=(70, 46, 8, 255), width=ss)
    # завитки-скобки по бокам
    for sgn in (-1, 1):
        xc = cx + fx * int(R * 0.45) + sgn * int(R * 0.30)
        d.arc([xc - int(R * 0.4), cy - int(R * 0.8), xc + int(R * 0.4), cy + int(R * 0.8)],
              90, 270, fill=(216, 178, 86, 255), width=max(2, int(R * 0.12)))
    # центральный овал
    d.ellipse([cx - int(R * 0.42), cy - int(R * 0.62), cx + int(R * 0.42), cy + int(R * 0.62)],
              fill=(238, 208, 122, 255), outline=(120, 82, 22, 255), width=ss)
    d.ellipse([cx - int(R * 0.24), cy - int(R * 0.34), cx + int(R * 0.24), cy + int(R * 0.34)],
              fill=(250, 226, 152, 255))
    _ramka_bead(d, cx, cy, max(2, int(R * 0.14)))


def _ramka_draw(pw: int, ph: int) -> Image.Image:
    """Орнаментальная золотая рамка под размер фото (pw×ph), центр прозрачный."""
    SS = 2  # supersampling: рисуем в 2 раза крупнее и сглаживаем
    W, H = pw * SS, ph * SS
    b = max(26, int(min(pw, ph) * 0.155)) * SS
    b = min(b, min(W, H) // 2 - 8 * SS)
    if b <= 0:
        b = max(2, min(W, H) // 3)
    r_out = max(2, int(b * 0.38))

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # 1. Тело рамки — золотой градиент с мягким скруглением
    body_grad = _ramka_gradient(W, H, [
        (0.00, (252, 236, 176)),
        (0.15, (238, 206, 122)),
        (0.42, (206, 162, 72)),
        (0.68, (180, 132, 46)),
        (0.86, (150, 102, 34)),
        (1.00, (112, 72, 20)),
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
    d.rounded_rectangle([SS, SS, W - 1 - SS, int(b * 0.30)], radius=r_out,
                        fill=(255, 246, 208, 70))
    d.rounded_rectangle([SS, H - 1 - int(b * 0.22), W - 1 - SS, H - 1 - SS], radius=r_out,
                        fill=(58, 36, 6, 90))

    # двойной филлет у отверстия — две тонкие линии (признак дорогой рамки)
    fl = int(b * 0.60)
    d.rounded_rectangle([fl, fl, W - 1 - fl, H - 1 - fl], radius=max(2, r_out - fl),
                        outline=(255, 244, 200, 220), width=SS)
    d.rounded_rectangle([fl + SS * 2, fl + SS * 2, W - 1 - fl - SS * 2, H - 1 - fl - SS * 2],
                        radius=max(2, r_out - fl - SS * 2),
                        outline=(88, 58, 14, 200), width=SS)

    # флейты — резные бороздки вдоль молдингов (перекрываются орнаментами)
    fstep = max(2, int(b * 0.05))
    fcnt = max(2, int((b * 0.32) / fstep))
    _ramka_flute_h(d, 0, W - 1, int(b * 0.08), fcnt, fstep)
    _ramka_flute_h(d, 0, W - 1, H - int(b * 0.40), fcnt, fstep)
    _ramka_flute_v(d, 0, H - 1, int(b * 0.08), fcnt, fstep)
    _ramka_flute_v(d, 0, H - 1, W - int(b * 0.40), fcnt, fstep)

    # освещение канала: тень на верхней стенке, блик на нижней
    cb = [i1, i1, W - 1 - i1, H - 1 - i1]
    d.arc(cb, 0, 180, fill=(88, 60, 14, 200), width=max(2, SS * 2))
    d.arc(cb, 180, 360, fill=(255, 238, 190, 220), width=max(2, SS * 2))

    # углы — барочные волюты со спиралями
    cc = int(b * 0.55)
    cr = int(b * 0.42)
    _ramka_baroque_corner(d, cc, cc, cr, SS, 1, 1)
    _ramka_baroque_corner(d, W - cc, cc, cr, SS, -1, 1)
    _ramka_baroque_corner(d, cc, H - cc, cr, SS, 1, -1)
    _ramka_baroque_corner(d, W - cc, H - cc, cr, SS, -1, -1)

    # раковины-вееры по центру верха и низа
    _ramka_shell_crest(d, W // 2, int(b * 0.58), int(b * 0.32), SS, 1)
    _ramka_shell_crest(d, W // 2, H - int(b * 0.58), int(b * 0.32), SS, -1)

    # свитки-картуши по центру боковин
    _ramka_side_cartouche(d, cc, H // 2, int(b * 0.30), SS, -1)
    _ramka_side_cartouche(d, W - cc, H // 2, int(b * 0.30), SS, 1)

    # обрезаем орнаменты по форме тела рамки
    orn.putalpha(ImageChops.multiply(orn.getchannel("A"), body_mask))

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

    # 5. Прозрачное отверстие в центре — сквозь него видно фото целиком.
    #    Отступ hpad сохраняет жемчуг и тёмную кромку по периметру отверстия.
    #    (Страховка от инверсии координат на абсурдно маленьких фото: если
    #    отверстие не влезает — оставляем рамку без выреза, падать не будем.)
    hpad = max(int(rb * 1.7), int(b * 0.13)) + SS * 2
    hx0, hy0, hx1, hy1 = ox0 + hpad, oy0 + hpad, ox1 - hpad, oy1 - hpad
    hole_r = max(2, int(b * 0.10))
    if hx1 > hx0 and hy1 > hy0:
        hole = Image.new("L", (W, H), 255)
        ImageDraw.Draw(hole).rounded_rectangle(
            [hx0, hy0, hx1, hy1], radius=hole_r, fill=0
        )
        img.putalpha(ImageChops.multiply(img.getchannel("A"), hole))

        # 6. Мягкая тень на фото сразу под рамкой — эффект «вставленного» фото
        sh_w = max(2, int(b * 0.13))
        ring = Image.new("L", (W, H), 0)
        rd = ImageDraw.Draw(ring)
        rd.rounded_rectangle([hx0, hy0, hx1, hy1], radius=hole_r, fill=255)
        rd.rounded_rectangle(
            [hx0 + sh_w, hy0 + sh_w, hx1 - sh_w, hy1 - sh_w],
            radius=max(2, hole_r - sh_w), fill=0,
        )
        ring = ring.filter(ImageFilter.GaussianBlur(max(1, int(sh_w * 0.55))))
        img.alpha_composite(Image.composite(
            Image.new("RGBA", (W, H), (0, 0, 0, 175)),
            Image.new("RGBA", (W, H), (0, 0, 0, 0)),
            ring,
        ))

    # 7. Чёткие кромки: тёмная по отверстию + контур тела + блик по краю
    trim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    td = ImageDraw.Draw(trim)
    td.rectangle([ox0, oy0, ox1, oy1], outline=(84, 56, 10, 255), width=max(2, SS * 2))
    td.rounded_rectangle([0, 0, W - 1, H - 1], radius=r_out,
                         outline=(72, 46, 8, 255), width=max(2, SS * 2))
    # блик по верхнему и левому краю отверстия, тень по нижнему и правому
    td.line([ox0 + SS * 2, oy0 + SS, ox1 - SS * 2, oy0 + SS],
            fill=(255, 244, 200, 220), width=SS)
    td.line([ox0 + SS, oy0 + SS * 2, ox0 + SS, oy1 - SS * 2],
            fill=(255, 244, 200, 220), width=SS)
    td.line([ox0 + SS * 2, oy1 - SS, ox1 - SS * 2, oy1 - SS],
            fill=(88, 58, 14, 200), width=SS)
    td.line([ox1 - SS, oy0 + SS * 2, ox1 - SS, oy1 - SS * 2],
            fill=(88, 58, 14, 200), width=SS)
    img.alpha_composite(trim)

    return img.resize((pw, ph), Image.LANCZOS)


# ── Рамка из PNG по ссылке (RAMKA_URL) с фолбэком на рисованную ──────
# Рамка качается один раз и кэшируется на 10 минут: GitHub не дёргается
# на каждое фото. Ссылки нет или скачать не вышло — рисуем кодом.
_RAMKA_PNG: Optional[tuple] = None   # (RGBA-рамка, отверстие-или-None)
_RAMKA_PNG_TS: float = 0.0
_RAMKA_PNG_TTL = 600.0
_RAMKA_PNG_FAIL_TS: float = 0.0      # время последней неудачной загрузки
_RAMKA_URL_EMPTY_LOGGED = False      # лог «RAMKA_URL пуста» пишем один раз, не на каждое фото


def _ramka_find_hole(alpha: Image.Image):
    """Прямоугольник прозрачного отверстия в центре рамки (или None).

    Расширяем прямоугольник от центра, пока по его краям всё прозрачно —
    останавливаемся у первого непрозрачного (золотого) пикселя.
    """
    w, h = alpha.size
    a = alpha.load()
    cx, cy = w // 2, h // 2
    if a[cx, cy] > 8:
        return None  # центр не прозрачный — отверстия нет
    x0, y0, x1, y1 = cx, cy, cx, cy
    while True:
        grew = False
        if x0 > 0 and all(a[x0 - 1, y] <= 8 for y in range(y0, y1 + 1)):
            x0 -= 1
            grew = True
        if x1 < w - 1 and all(a[x1 + 1, y] <= 8 for y in range(y0, y1 + 1)):
            x1 += 1
            grew = True
        if y0 > 0 and all(a[x, y0 - 1] <= 8 for x in range(x0, x1 + 1)):
            y0 -= 1
            grew = True
        if y1 < h - 1 and all(a[x, y1 + 1] <= 8 for x in range(x0, x1 + 1)):
            y1 += 1
            grew = True
        if not grew:
            break
    return (x0, y0, x1, y1)


def _ramka_prepare_png(data: bytes):
    """Загружает PNG-рамку из байтов: RGBA, приведение размера, поиск отверстия."""
    try:
        im = Image.open(BytesIO(data))
        im.load()
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGBA":
            im = im.convert("RGBA")
        w, h = im.size
        if max(w, h) > 1920:
            im.thumbnail((1920, 1920), Image.LANCZOS)
        elif max(w, h) < 1080:
            # мелкие рамки увеличиваем, чтобы результат не был пиксельным
            s = 1080 / max(w, h)
            im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
        return (im, _ramka_find_hole(im.getchannel("A")))
    except Exception as e:
        log.warning(f"ramka png prepare: {e}")
        return None


async def _ramka_load_png():
    """Скачивает рамку из RAMKA_URL (кэш 10 минут). None — рамки нет / ошибка.

    Неудачные попытки тоже запоминаются на 5 минут — битая ссылка не будет
    тормозить каждый вызов .ramka сетевым запросом.
    """
    global _RAMKA_PNG, _RAMKA_PNG_TS, _RAMKA_PNG_FAIL_TS, _RAMKA_URL_EMPTY_LOGGED
    url = RAMKA_URL.strip()
    if not url:
        if not _RAMKA_URL_EMPTY_LOGGED:
            _RAMKA_URL_EMPTY_LOGGED = True
            log.info("ramka: RAMKA_URL пуста — рисую рамку кодом (барочный стиль)")
        return None
    now = time.monotonic()
    if _RAMKA_PNG is not None and now - _RAMKA_PNG_TS < _RAMKA_PNG_TTL:
        return _RAMKA_PNG
    if _RAMKA_PNG_FAIL_TS and now - _RAMKA_PNG_FAIL_TS < 300:
        return None
    try:
        session = get_http()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"download frame: status {resp.status}")
            data = await resp.read()
        pack = await asyncio.to_thread(_ramka_prepare_png, data)
        if pack is None:
            _RAMKA_PNG_FAIL_TS = now
            return None
        _RAMKA_PNG = pack
        _RAMKA_PNG_TS = now
        _RAMKA_PNG_FAIL_TS = 0.0
        frame, hole = pack
        log.info(
            f"ramka: PNG-рамка загружена ({frame.size[0]}x{frame.size[1]}, "
            f"отверстие: {'найдено' if hole else 'НЕТ — будет нарисована рамка кодом'})"
        )
        return pack
    except Exception as e:
        log.warning(f"ramka png load: {e}")
        _RAMKA_PNG_FAIL_TS = now
        return None


def _ramka_png_render(photo_rgb: Image.Image, frame: Image.Image, hole):
    """Фото заливает отверстие PNG-рамки целиком (cover-fit), рамка сверху.

    None — рамку применить нельзя (нет прозрачного отверстия) → рисуем кодом.
    """
    alpha = frame.getchannel("A")
    ob = alpha.getbbox()  # у рамки могут быть прозрачные края — обрезаем по ним
    if ob is None:
        return None
    ox0, oy0, ox1, oy1 = ob
    fw, fh = ox1 - ox0 + 1, oy1 - oy0 + 1
    if hole is None or fw < 4 or fh < 4:
        return None
    hx0, hy0, hx1, hy1 = hole
    hw, hh = hx1 - hx0 + 1, hy1 - hy0 + 1
    if hw < 1 or hh < 1:
        return None
    # cover-fit: масштабируем фото до размера отверстия и центрируем (края подрежутся)
    scale = max(hw / photo_rgb.width, hh / photo_rgb.height)
    p_w = max(hw, int(photo_rgb.width * scale))
    p_h = max(hh, int(photo_rgb.height * scale))
    photo = photo_rgb.resize((p_w, p_h), Image.LANCZOS)
    left = (p_w - hw) // 2
    top = (p_h - hh) // 2
    photo = photo.crop((left, top, left + hw, top + hh)).convert("RGBA")
    canvas = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
    canvas.paste(photo, (hx0 - ox0, hy0 - oy0))
    canvas.alpha_composite(frame.crop(ob))
    return canvas


def _ramka_render(data: bytes, frame_pack: Optional[tuple]) -> bytes:
    """Синхронно (в потоке): фото → фото в рамке, JPEG-байты.

    frame_pack = (RGBA-рамка, отверстие) из RAMKA_URL, либо None → рисованная рамка.
    """
    im = Image.open(BytesIO(data))
    im = ImageOps.exif_transpose(im).convert("RGB")
    if im.width * im.height > 4_000_000:
        im.thumbnail((1920, 1920), Image.LANCZOS)
    if frame_pack is not None:
        out = _ramka_png_render(im, frame_pack[0], frame_pack[1])
        if out is None:
            log.warning("ramka: PNG-рамка без отверстия — рисую рамку кодом")
            frame = _ramka_draw(im.width, im.height)
            out = im.convert("RGBA")
            out.alpha_composite(frame)
    else:
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
    frame_pack = await _ramka_load_png()
    return await asyncio.to_thread(_ramka_render, data, frame_pack)


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
    thinking = await send_fn("◆ · · ·")
    ok = await _ramka_run(photo, chat_id, business_connection_id=business_connection_id)
    await _ramka_cleanup(thinking)
    if not ok:
        await send_fn("◇ Не получилось наложить рамку — попробуй другое фото.")
    return ok


def _ramka_reply_photo(msg: Message) -> Optional[PhotoSize]:
    r = msg.reply_to_message
    if r and r.photo:
        return r.photo[-1]
    return None


_RAMKA_HINT = (
    f"◇ <b>.ramka</b> — ответь на чьё-то <b>фото</b> и напиши <code>.ramka</code>,\n"
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
        f"◆ <b>ЗОЛОТАЯ РАМКА</b>\n<code>{LINE}</code>\n\n"
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
            f"◇ <b>.ramka</b> — ответь на чьё-то <b>фото</b> и напиши <code>.ramka</code>.\n\n"
            f"— 👁️ @{BOT_USERNAME}",
        )
        return
    ok = await _business_edit_message(conn_id, msg.chat.id, msg.message_id, "◆ · · ·")
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
            "◇ Не получилось наложить рамку — попробуй другое фото.",
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
