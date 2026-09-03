"""Детерминированный подбор двух команд по ELO для очереди."""

from itertools import combinations

import config
from database.models import User


def balance_teams(players: list[User]) -> tuple[list[User], list[User]]:
    """Возвращает две команды равного размера с минимальной разницей ELO.

    Размер матча берётся из ``config.PLAYERS_PER_GAME`` (по умолчанию 10 → 5v5).
    Для чётного N перебираются C(N, N/2) комбинаций; при N=10 это 252,
    поэтому алгоритм остаётся точным, быстрым и предсказуемым. При равенстве
    суммы ELO выигрывает набор с лексикографически меньшими внутренними ID.

    Raises:
        ValueError: если число игроков ≠ PLAYERS_PER_GAME или оно нечётное
            (равные команды невозможны).
    """
    n = len(players)
    expected = config.PLAYERS_PER_GAME
    if n != expected:
        raise ValueError(
            f"matchmaking requires exactly {expected} players, got {n}"
        )
    if n % 2 != 0:
        raise ValueError(
            f"matchmaking requires an even number of players for equal teams, got {n}"
        )
    team_size = n // 2
    ordered = sorted(players, key=lambda player: player.id)
    total = sum(player.elo for player in ordered)
    best: tuple[tuple[int, tuple[int, ...]], list[User]] | None = None
    for candidate in combinations(ordered, team_size):
        key = (
            abs(sum(player.elo for player in candidate) * 2 - total),
            tuple(player.id for player in candidate),
        )
        if best is None or key < best[0]:
            best = (key, list(candidate))
    assert best is not None
    blue = best[1]
    blue_ids = {player.id for player in blue}
    red = [player for player in ordered if player.id not in blue_ids]
    return blue, red
