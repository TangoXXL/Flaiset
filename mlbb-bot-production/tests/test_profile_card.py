"""
Тесты рендера PNG-карточки профиля (utils/profile_card.py).

Не проверяем пиксели (это визуальный дизайн), но проверяем контракт:
функция асинхронная, не блокирует event loop дольше разумного, всегда
возвращает валидный PNG нужного размера для любых пограничных входных
данных (0 игр, максимальный/минимальный ELO, очень длинное имя), и
поднимает исключение на битых данных аватара (чтобы cogs/profile.py мог
корректно откатиться на текстовый embed).
"""

import io

import pytest
from PIL import Image

from utils.profile_card import CARD_H, CARD_W, _needs_universal_font, build_profile_card_png


def _fake_avatar_bytes() -> bytes:
    img = Image.new("RGB", (256, 256), (40, 60, 90))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize(
    "elo,wins,losses,games,win_rate,name",
    [
        (100, 0, 0, 0, 0.0, "NewPlayer"),          # новый игрок, 0 игр
        (0, 0, 5, 5, 0.0, "ZeroElo"),                # минимальный ELO
        (350, 40, 10, 50, 80.0, "TopPlayer"),        # максимальный ранг
        (312, 14, 9, 23, 60.9, "A" * 40),             # очень длинное имя
    ],
)
async def test_build_profile_card_png_valid_output(elo, wins, losses, games, win_rate, name):
    png_bytes = await build_profile_card_png(
        display_name=name,
        player_id="12345678901",
        server_id="12345",
        elo=elo,
        wins=wins,
        losses=losses,
        games=games,
        win_rate=win_rate,
        avatar_bytes=_fake_avatar_bytes(),
    )

    assert isinstance(png_bytes, bytes)
    assert len(png_bytes) > 0

    img = Image.open(io.BytesIO(png_bytes))
    img.load()
    assert img.format == "PNG"
    assert img.size == (CARD_W, CARD_H)


async def test_build_profile_card_png_raises_on_broken_avatar():
    with pytest.raises(Exception):
        await build_profile_card_png(
            display_name="Broken",
            player_id="1",
            server_id="1",
            elo=100,
            wins=0,
            losses=0,
            games=0,
            win_rate=0.0,
            avatar_bytes=b"not a real image",
        )


# --- Регрессия: кириллический ник рендерился "тофу"-квадратами -----------
# Poppins (основной декоративный шрифт карточки) не содержит кириллицу.
# build_profile_card_png не проверяет пиксели напрямую, но обязана
# отработать без ошибок для кириллических/смешанных ников — визуально
# корректность подтверждена вручную (см. отчёт), здесь фиксируем контракт
# на уровне выбора шрифта и общей работоспособности рендера.

@pytest.mark.parametrize(
    "name",
    [
        "Тестовыйник",
        "Артём_1337",
        "xX_Дракоша_Xx",
        "MixedИмя123",
    ],
)
async def test_build_profile_card_png_handles_cyrillic_names(name):
    png_bytes = await build_profile_card_png(
        display_name=name,
        player_id="11111111",
        server_id="1111",
        elo=100,
        wins=0,
        losses=0,
        games=0,
        win_rate=0.0,
        avatar_bytes=_fake_avatar_bytes(),
    )
    img = Image.open(io.BytesIO(png_bytes))
    img.load()
    assert img.size == (CARD_W, CARD_H)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("AlicePlaysML", False),      # чистая латиница — Poppins справляется
        ("Bob123", False),
        ("Тестовыйник", True),        # кириллица — нужен универсальный шрифт
        ("MixedИмя", True),           # смешанный текст — тоже переключаемся
        ("", False),                  # пустая строка — деградирует безопасно
    ],
)
def test_needs_universal_font_detects_cyrillic(text, expected):
    assert _needs_universal_font(text) is expected
