"""
Ранговая система на основе ELO.

Это косметическая надстройка поверх числового ELO (актуальное ТЗ, п.12) —
она НЕ участвует в начислении/списании очков и никак не влияет на логику
игр/драфта/результатов. Используется только для визуального представления
в /profile (карточка), /leaderboard и /matches: цвет, название ранга,
бейдж и прогресс до следующего ранга.

Названия рангов вдохновлены реальной системой Mobile Legends, но пороги
подобраны под локальную ELO-шкалу бота (старт 100, +25/-25 за игру,
минимум 0, см. config.py).
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RankTier:
    name: str                          # полное имя ранга
    short: str                         # 1-2 буквы для компактного бейджа
    min_elo: int                       # ELO, с которого начинается ранг
    color: tuple[int, int, int]        # основной цвет ранга (RGB)
    color_dark: tuple[int, int, int]   # тёмный оттенок для градиентов/фона


# Пороги идут по возрастанию ELO. Порядок важен — get_rank/get_next_rank
# опираются на то, что список отсортирован по min_elo.
RANKS: list[RankTier] = [
    RankTier("Warrior",        "W",  0,   (150, 158, 168), (68, 74, 82)),
    RankTier("Elite",          "E",  50,  (69, 199, 150),  (24, 84, 64)),
    RankTier("Master",         "M",  100, (68, 146, 244),  (24, 62, 116)),
    RankTier("Grandmaster",    "GM", 150, (161, 97, 229),  (66, 36, 102)),
    RankTier("Epic",           "EP", 200, (232, 69, 143),  (100, 28, 62)),
    RankTier("Legend",         "L",  250, (255, 148, 40),  (128, 66, 14)),
    RankTier("Mythic",         "MY", 300, (238, 58, 58),   (112, 22, 22)),
    RankTier("Mythical Glory", "MG", 350, (255, 208, 64),  (132, 96, 8)),
]


def get_rank(elo: int) -> RankTier:
    """Возвращает ранг, соответствующий данному ELO."""
    current = RANKS[0]
    for tier in RANKS:
        if elo >= tier.min_elo:
            current = tier
        else:
            break
    return current


def get_next_rank(elo: int) -> Optional[RankTier]:
    """Следующий ранг после текущего, либо None если это максимальный ранг."""
    current = get_rank(elo)
    idx = RANKS.index(current)
    if idx + 1 < len(RANKS):
        return RANKS[idx + 1]
    return None


def rank_progress(elo: int) -> tuple[RankTier, Optional[RankTier], float]:
    """
    Возвращает (текущий_ранг, следующий_ранг_или_None, доля_прогресса 0..1)
    — насколько игрок продвинулся от начала текущего ранга к следующему.
    Для максимального ранга прогресс всегда 1.0 и next_tier is None.
    """
    current = get_rank(elo)
    nxt = get_next_rank(elo)
    if nxt is None:
        return current, None, 1.0
    span = nxt.min_elo - current.min_elo
    progress = (elo - current.min_elo) / span if span > 0 else 1.0
    return current, nxt, max(0.0, min(1.0, progress))


def rank_badge_text(elo: int) -> str:
    """Короткая текстовая метка ранга для embed'ов, например '🔷 Master'."""
    tier = get_rank(elo)
    return f"{tier.name}"
