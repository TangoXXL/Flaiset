"""Сервис Valhalla Coin и Elo Protection.

Все изменения баланса идут через Database.*_vc / purchase_* —
этот модуль задаёт правила наград и валидацию ставок.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import config


@dataclass(frozen=True)
class MatchReward:
    vc: int
    elo_delta: int
    protected: bool


def roll_match_vc(won: bool) -> int:
    if won:
        return random.randint(config.WIN_COINS_MIN, config.WIN_COINS_MAX)
    return random.randint(config.LOSS_COINS_MIN, config.LOSS_COINS_MAX)


def match_rewards_batch(winner_count: int, loser_count: int) -> tuple[list[int], list[int]]:
    """Одинаковый VC на команду не требуется — каждому свой roll."""
    wins = [roll_match_vc(True) for _ in range(winner_count)]
    losses = [roll_match_vc(False) for _ in range(loser_count)]
    return wins, losses
