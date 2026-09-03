"""
Тесты ранговой системы (utils/rank.py) — косметическая надстройка над ELO,
не влияет на его начисление, но должна корректно определять тир, следующий
тир и прогресс к нему на всех границах.
"""

import pytest

from utils.rank import RANKS, get_next_rank, get_rank, rank_progress


def test_ranks_sorted_by_min_elo():
    thresholds = [tier.min_elo for tier in RANKS]
    assert thresholds == sorted(thresholds)
    assert thresholds[0] == 0  # первый тир обязан покрывать elo=0 (минимум ELO)


@pytest.mark.parametrize(
    "elo,expected_name",
    [
        (0, "Warrior"),
        (49, "Warrior"),
        (50, "Elite"),
        (99, "Elite"),
        (100, "Master"),   # стартовый ELO новых игроков (config.py)
        (149, "Master"),
        (150, "Grandmaster"),
        (349, "Mythic"),
        (350, "Mythical Glory"),
        (10_000, "Mythical Glory"),  # выше последнего порога — тот же максимальный тир
    ],
)
def test_get_rank_boundaries(elo, expected_name):
    assert get_rank(elo).name == expected_name


def test_get_next_rank_for_top_tier_is_none():
    assert get_next_rank(350) is None
    assert get_next_rank(10_000) is None


def test_get_next_rank_regular():
    nxt = get_next_rank(100)  # Master -> Grandmaster
    assert nxt is not None
    assert nxt.name == "Grandmaster"


def test_rank_progress_start_of_tier_is_zero():
    tier, nxt, progress = rank_progress(100)  # ровно граница Master
    assert tier.name == "Master"
    assert nxt.name == "Grandmaster"
    assert progress == 0.0


def test_rank_progress_middle_of_tier():
    # Master: 100..149 (span 50). elo=125 -> прогресс 0.5
    tier, nxt, progress = rank_progress(125)
    assert tier.name == "Master"
    assert progress == pytest.approx(0.5)


def test_rank_progress_top_tier_is_maxed_out():
    tier, nxt, progress = rank_progress(500)
    assert tier.name == "Mythical Glory"
    assert nxt is None
    assert progress == 1.0


def test_rank_progress_never_out_of_bounds():
    for elo in range(-10, 500, 7):
        _, _, progress = rank_progress(max(elo, 0))
        assert 0.0 <= progress <= 1.0
