"""
Генерация PNG-карточки профиля игрока (визуальное улучшение /profile).

Карточка полностью рисуется на сервере через Pillow — фон с градиентом и
шестигранным узором в цвете ранга, круглый аватар пользователя с рамкой,
шестиугольный бейдж ранга, крупный ELO, статы (Игр/Побед/Поражений/WinRate)
и прогресс-бар до следующего ранга. Ранговая система — utils/rank.py,
она не влияет на реальный ELO, только на цвет/название/бейдж.

Рендер — синхронный и CPU-bound (Pillow), поэтому build_profile_card_file
всегда выполняет его в отдельном потоке через run_in_executor, чтобы не
блокировать event loop бота во время игр/драфта.

Если шрифты или Pillow недоступны/сломаны, build_profile_card_file
поднимает исключение — вызывающий код (cogs/profile.py) должен на этот
случай иметь fallback на обычный текстовый embed, чтобы /profile не падал
целиком из-за проблем с рендером картинки.
"""

import asyncio
import io
import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from utils.rank import rank_progress

# --- Шрифты --------------------------------------------------------------
# Порядок: сначала шрифты, забандленные с ботом (assets/fonts) — они есть
# всегда, независимо от того, что установлено на сервере хостинга; затем
# несколько типичных системных путей на всякий случай; в самом конце —
# растровый шрифт Pillow по умолчанию, чтобы карточка в любом случае
# отрендерилась, пусть и не так красиво.
_ASSETS_FONTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")

_CANDIDATES_BOLD = [
    os.path.join(_ASSETS_FONTS, "Poppins-Bold.ttf"),
    "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_CANDIDATES_MEDIUM = [
    os.path.join(_ASSETS_FONTS, "Poppins-Medium.ttf"),
    "/usr/share/fonts/truetype/google-fonts/Poppins-Medium.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_CANDIDATES_COND_BOLD = [
    os.path.join(_ASSETS_FONTS, "DejaVuSansCondensed-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    os.path.join(_ASSETS_FONTS, "Poppins-Bold.ttf"),
]
# Poppins — латинский шрифт (Google Fonts), в нём НЕТ кириллицы: любой
# кириллический символ рендерится "тофу"-квадратом (см. баг с исчезающим
# ником в кириллице). Discord-ник — произвольный пользовательский текст,
# поэтому для него отдельно держим шрифт с широким покрытием Unicode
# (кириллица, расширенная латиница и т.д.) и переключаемся на него, если
# в тексте есть символы вне базовой латиницы Poppins.
_CANDIDATES_UNIVERSAL_BOLD = [
    os.path.join(_ASSETS_FONTS, "DejaVuSans-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    os.path.join(_ASSETS_FONTS, "Poppins-Bold.ttf"),
]

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _resolve_font_path(candidates: list[str]) -> str | None:
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _load_font(candidates: list[str], size: int) -> ImageFont.ImageFont:
    path = _resolve_font_path(candidates)
    cache_key = (path or "__default__", size)
    cached = _font_cache.get(cache_key)
    if cached is not None:
        return cached

    font: ImageFont.ImageFont
    if path is not None:
        try:
            font = ImageFont.truetype(path, size)
        except OSError:
            font = ImageFont.load_default(size=size)
    else:
        font = ImageFont.load_default(size=size)

    _font_cache[cache_key] = font
    return font


SCALE = 2  # супер-сэмплинг: рисуем в 2x и уменьшаем в конце для сглаживания
CARD_W, CARD_H = 1000, 360
W, H = CARD_W * SCALE, CARD_H * SCALE


def _font_bold(size: int) -> ImageFont.ImageFont:
    return _load_font(_CANDIDATES_BOLD, size * SCALE)


def _font_universal_bold(size: int) -> ImageFont.ImageFont:
    return _load_font(_CANDIDATES_UNIVERSAL_BOLD, size * SCALE)


def _needs_universal_font(text: str) -> bool:
    """True, если в тексте есть символы за пределами базовой латиницы
    Poppins (например, кириллица) — тогда нужен шрифт с более широким
    покрытием Unicode, иначе символы отрендерятся как тофу-квадраты."""
    return any(ord(ch) > 0x02AF for ch in text)


def _font_medium(size: int) -> ImageFont.ImageFont:
    return _load_font(_CANDIDATES_MEDIUM, size * SCALE)


def _font_cond_bold(size: int) -> ImageFont.ImageFont:
    return _load_font(_CANDIDATES_COND_BOLD, size * SCALE)


# --- Низкоуровневые графические помощники ---------------------------------

def _lerp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def _vertical_gradient(size, top_color, bottom_color):
    w, h = size
    col = Image.new("RGB", (1, h))
    for y in range(h):
        col.putpixel((0, y), _lerp_color(top_color, bottom_color, y / max(1, h - 1)))
    return col.resize((w, h))


def _glow(size, color, center, radius, alpha=140):
    """Мягкое радиальное свечение как отдельный RGBA-слой (для alpha_composite)."""
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse(
        [center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius], fill=alpha
    )
    mask = mask.filter(ImageFilter.GaussianBlur(radius * 0.55))
    solid = Image.new("RGBA", size, (*color, 255))
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    return Image.composite(solid, out, mask)


def _hexagon(cx, cy, r, rot=90):
    return [
        (cx + r * math.cos(math.radians(60 * i - rot)), cy + r * math.sin(math.radians(60 * i - rot)))
        for i in range(6)
    ]


def _hex_pattern_layer(size, color, alpha, r=32, gap=8):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    step_x = (r * 2 + gap) * 0.87
    step_y = (r * 1.8 + gap) * 0.75
    row, y = 0, -r
    w, h = size
    while y < h + r:
        offset = step_x / 2 if row % 2 else 0
        x = -r + offset
        while x < w + r:
            d.polygon(_hexagon(x, y, r), outline=(*color, alpha), width=2)
            x += step_x
        y += step_y
        row += 1
    return layer


def _circle_mask(size):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size[0] - 1, size[1] - 1], fill=255)
    return mask


def _fit_font(draw, text, font_fn, max_size, min_size, max_w_scaled):
    size = max_size
    while size > min_size:
        f = font_fn(size)
        if draw.textlength(text, font=f) <= max_w_scaled:
            return f
        size -= 2
    return font_fn(min_size)


def _truncate(draw, text, f, max_w_scaled):
    if draw.textlength(text, font=f) <= max_w_scaled:
        return text
    while text and draw.textlength(text + "…", font=f) > max_w_scaled:
        text = text[:-1]
    return (text + "…") if text else "…"


def _stat_block(ui, x, y, w, h, value, label, accent):
    d = ImageDraw.Draw(ui, "RGBA")
    d.rounded_rectangle([x, y, x + w, y + h], radius=14 * SCALE, fill=(255, 255, 255, 18))
    d.rounded_rectangle([x, y, x + 6 * SCALE, y + h], radius=3 * SCALE, fill=(*accent, 255))

    vf = _font_bold(25)
    vw = d.textlength(value, font=vf)
    d.text((x + w / 2 - vw / 2, y + 9 * SCALE), value, font=vf, fill=(255, 255, 255, 255))

    lf = _font_cond_bold(12)
    label_up = label.upper()
    lw = d.textlength(label_up, font=lf)
    d.text((x + w / 2 - lw / 2, y + h - 23 * SCALE), label_up, font=lf, fill=(172, 178, 190, 255))


def _render_sync(
    display_name: str,
    player_id: str,
    server_id: str,
    elo: int,
    wins: int,
    losses: int,
    games: int,
    win_rate: float,
    avatar_img: Image.Image,
) -> Image.Image:
    tier, next_tier, progress = rank_progress(elo)

    # --- Слой 1: фон (полностью непрозрачный) ---------------------------
    bg = _vertical_gradient((W, H), (23, 25, 35), (13, 14, 20)).convert("RGBA")
    bg.alpha_composite(_hex_pattern_layer((W, H), tier.color, alpha=26))
    bg.alpha_composite(_glow((W, H), tier.color, (int(0.22 * W), int(0.5 * H)), radius=int(0.46 * H), alpha=110))

    stripe = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(stripe).polygon(
        [(W * 0.60, 0), (W * 0.70, 0), (W * 0.42, H), (W * 0.32, H)], fill=(255, 255, 255, 12)
    )
    bg.alpha_composite(stripe)

    border = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(border).rounded_rectangle(
        [3 * SCALE, 3 * SCALE, W - 3 * SCALE, H - 3 * SCALE],
        radius=28 * SCALE, outline=(*tier.color, 170), width=3 * SCALE,
    )
    bg.alpha_composite(border)

    # --- Слой 2: UI (аватар, текст, статы) — единый прозрачный слой -----
    # ВАЖНО: полупрозрачные фигуры рисуются именно на этом отдельном
    # прозрачном слое, а не поверх уже непрозрачного bg — ImageDraw не
    # блендит альфа-канал при рисовании поверх непрозрачного изображения,
    # он просто перезаписывает пиксели. Правильное смешение с фоном даёт
    # только Image.alpha_composite(bg, ui) в самом конце.
    ui = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(ui, "RGBA")

    avatar_size = 168 * SCALE
    ax, ay = 56 * SCALE, (H - avatar_size) // 2
    ring_pad = 10 * SCALE
    ring_size = avatar_size + ring_pad * 2
    rx, ry = ax - ring_pad, ay - ring_pad

    ui.alpha_composite(_glow((W, H), tier.color, (rx + ring_size // 2, ry + ring_size // 2), radius=ring_size, alpha=130))

    ring = Image.new("RGBA", (ring_size, ring_size), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    rd.ellipse([0, 0, ring_size, ring_size], fill=(*tier.color_dark, 255))
    rd.ellipse(
        [ring_pad * 0.4, ring_pad * 0.4, ring_size - ring_pad * 0.4, ring_size - ring_pad * 0.4],
        outline=(*tier.color, 255), width=int(3 * SCALE),
    )
    ui.alpha_composite(ring, (rx, ry))

    avatar_fit = ImageOps.fit(avatar_img.convert("RGB"), (avatar_size, avatar_size)).convert("RGBA")
    avatar_fit.putalpha(_circle_mask((avatar_size, avatar_size)))
    ui.alpha_composite(avatar_fit, (ax, ay))

    badge_r = 34 * SCALE
    bcx, bcy = ax + avatar_size - 8 * SCALE, ay + avatar_size - 8 * SCALE
    draw.polygon(_hexagon(bcx, bcy, badge_r + 5 * SCALE), fill=(18, 19, 26, 255))
    draw.polygon(_hexagon(bcx, bcy, badge_r), fill=(*tier.color, 255))
    short_f = _font_bold(20 if len(tier.short) > 1 else 24)
    sw = draw.textlength(tier.short, font=short_f)
    sbbox = draw.textbbox((0, 0), tier.short, font=short_f)
    sh = sbbox[3] - sbbox[1]
    draw.text((bcx - sw / 2, bcy - sh / 2 - sbbox[1]), tier.short, font=short_f, fill=(255, 255, 255, 255))

    content_x = ax + avatar_size + 46 * SCALE
    content_w = W - content_x - 48 * SCALE

    # Верхний правый блок: ранг (сверху) + ELO (крупно, под ним)
    rank_f = _font_medium(18)
    rank_text = tier.name.upper()
    rw = draw.textlength(rank_text, font=rank_f)
    draw.text((content_x + content_w - rw, 26 * SCALE), rank_text, font=rank_f, fill=(*tier.color, 255))

    elo_f = _font_bold(48)
    elo_text = str(elo)
    ew = draw.textlength(elo_text, font=elo_f)
    elo_y = 26 * SCALE + 26 * SCALE
    draw.text((content_x + content_w - ew, elo_y), elo_text, font=elo_f, fill=(255, 255, 255, 255))

    # Имя и ID — слева, ограничены по ширине, чтобы не наезжать на ELO-блок
    name_max_w = content_w - ew - 24 * SCALE
    name_font_fn = _font_universal_bold if _needs_universal_font(display_name) else _font_bold
    name_f = _fit_font(draw, display_name, name_font_fn, 34, 20, name_max_w)
    name_text = _truncate(draw, display_name, name_f, name_max_w)
    draw.text((content_x, 30 * SCALE), name_text, font=name_f, fill=(255, 255, 255, 255))

    sub_f = _font_cond_bold(14)
    sub_text = f"ID {player_id}  •  SERVER {server_id}"
    draw.text((content_x, 30 * SCALE + 44 * SCALE), sub_text, font=sub_f, fill=(150, 156, 168, 255))

    divider_y = 118 * SCALE
    draw.line(
        [(content_x, divider_y), (content_x + content_w, divider_y)],
        fill=(255, 255, 255, 40), width=int(1.5 * SCALE),
    )

    stats_y = divider_y + 16 * SCALE
    stat_h = 64 * SCALE
    gap = 12 * SCALE
    stat_w = (content_w - gap * 3) / 4
    _stat_block(ui, content_x, stats_y, stat_w, stat_h, str(games), "Игр", (110, 150, 255))
    _stat_block(ui, content_x + (stat_w + gap), stats_y, stat_w, stat_h, str(wins), "Побед", (80, 214, 150))
    _stat_block(ui, content_x + 2 * (stat_w + gap), stats_y, stat_w, stat_h, str(losses), "Поражений", (232, 90, 90))
    _stat_block(ui, content_x + 3 * (stat_w + gap), stats_y, stat_w, stat_h, f"{win_rate:.0f}%", "Winrate", (255, 196, 60))

    bar_y = stats_y + stat_h + 26 * SCALE
    bar_h = 14 * SCALE
    draw.rounded_rectangle(
        [content_x, bar_y, content_x + content_w, bar_y + bar_h], radius=bar_h // 2, fill=(255, 255, 255, 26)
    )
    if progress > 0:
        fill_w = max(bar_h, content_w * progress)
        draw.rounded_rectangle(
            [content_x, bar_y, content_x + fill_w, bar_y + bar_h], radius=bar_h // 2, fill=(*tier.color, 255)
        )

    prog_f = _font_cond_bold(13)
    if next_tier is not None:
        prog_text = f"{elo - tier.min_elo} / {next_tier.min_elo - tier.min_elo}  ДО РАНГА {next_tier.name.upper()}"
    else:
        prog_text = "★ МАКСИМАЛЬНЫЙ РАНГ ★"
    draw.text((content_x, bar_y + bar_h + 8 * SCALE), prog_text, font=prog_f, fill=(190, 196, 206, 255))

    bg.alpha_composite(ui)
    return bg.convert("RGB").resize((CARD_W, CARD_H), Image.LANCZOS)


async def build_profile_card_png(
    display_name: str,
    player_id: str,
    server_id: str,
    elo: int,
    wins: int,
    losses: int,
    games: int,
    win_rate: float,
    avatar_bytes: bytes,
) -> bytes:
    """
    Асинхронная обёртка: декодирует аватар и рендерит карточку в отдельном
    потоке (Pillow — синхронный/CPU-bound), возвращает готовый PNG в виде
    bytes. Может поднять исключение (например, если avatar_bytes битые) —
    вызывающий код обязан отловить его и откатиться на обычный embed.
    """
    avatar_img = Image.open(io.BytesIO(avatar_bytes))
    avatar_img.load()  # форсируем декодирование внутри try вызывающего кода

    loop = asyncio.get_running_loop()
    result_img = await loop.run_in_executor(
        None,
        _render_sync,
        display_name, player_id, server_id, elo, wins, losses, games, win_rate, avatar_img,
    )

    buf = io.BytesIO()
    result_img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
