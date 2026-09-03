"""
Тесты создания игры, набора игроков и драфта капитанов — упор на гонки.
"""

import asyncio

import pytest


async def _make_players(db, count, start_id=1):
    users = []
    for i in range(start_id, start_id + count):
        u = await db.create_user_atomic(i, f"Player{i}", str(1000 + i), "1")
        users.append(u)
    return users


async def test_game_creation(db):
    game = await db.create_game_if_none_active(guild_id=42)
    assert game is not None
    assert game.status == "WAITING"
    assert game.guild_id == 42


async def test_second_game_rejected_while_one_active(db):
    first = await db.create_game_if_none_active(guild_id=42)
    assert first is not None

    second = await db.create_game_if_none_active(guild_id=42)
    assert second is None  # уже есть активная игра на этом guild_id


async def test_new_game_allowed_after_previous_completed(db):
    first = await db.create_game_if_none_active(guild_id=42)
    await db.update_game_status(first.game_id, "COMPLETED")

    second = await db.create_game_if_none_active(guild_id=42)
    assert second is not None
    assert second.game_id != first.game_id


async def test_concurrent_game_creation_only_one_wins(db):
    """Два администратора почти одновременно вызывают /game — должна
    создаться ровно одна активная игра на guild_id."""
    results = await asyncio.gather(
        db.create_game_if_none_active(guild_id=1),
        db.create_game_if_none_active(guild_id=1),
        db.create_game_if_none_active(guild_id=1),
    )
    created = [g for g in results if g is not None]
    assert len(created) == 1


async def test_add_game_player_if_space_respects_limit(db):
    players = await _make_players(db, 3)
    game = await db.create_game_if_none_active(guild_id=1)

    for p in players:
        added = await db.add_game_player_if_space(game.game_id, p.id, max_players=2)
        assert added == (players.index(p) < 2)

    count = await db.count_game_players(game.game_id)
    assert count == 2


async def test_concurrent_joins_never_exceed_capacity(db):
    """10 игроков одновременно жмут 'Вступить' в набор на 10 мест —
    ни один не должен потеряться, и никто не должен попасть 11-м."""
    players = await _make_players(db, 12)
    game = await db.create_game_if_none_active(guild_id=1)

    results = await asyncio.gather(
        *[db.add_game_player_if_space(game.game_id, p.id, max_players=10) for p in players]
    )
    accepted = sum(1 for r in results if r)
    assert accepted == 10

    count = await db.count_game_players(game.game_id)
    assert count == 10


async def test_player_cannot_join_twice(db):
    players = await _make_players(db, 1)
    game = await db.create_game_if_none_active(guild_id=1)

    first = await db.add_game_player_if_space(game.game_id, players[0].id, max_players=10)
    second = await db.add_game_player_if_space(game.game_id, players[0].id, max_players=10)
    assert first is True
    assert second is False
    assert await db.count_game_players(game.game_id) == 1


async def test_transition_game_status_applies_once(db):
    """Атомарный переход WAITING -> DRAFT должен сработать только один
    раз, даже если вызван параллельно дважды (гонка закрытия набора при
    10/10, если два игрока присоединяются практически одновременно)."""
    game = await db.create_game_if_none_active(guild_id=1)

    results = await asyncio.gather(
        db.transition_game_status(game.game_id, "WAITING", "DRAFT"),
        db.transition_game_status(game.game_id, "WAITING", "DRAFT"),
    )
    assert sorted(results) == [False, True]

    updated = await db.get_game(game.game_id)
    assert updated.status == "DRAFT"


async def test_captain_pick_race_only_one_wins(db):
    """Два капитана (или два быстрых клика одного капитана) пытаются
    выбрать ОДНОГО И ТОГО ЖЕ игрока в разные команды одновременно —
    assign_team_if_available должен пропустить только один из вызовов."""
    players = await _make_players(db, 3)
    game = await db.create_game_if_none_active(guild_id=1)
    for p in players:
        await db.add_game_player_if_space(game.game_id, p.id, max_players=10)

    target = players[2]
    results = await asyncio.gather(
        db.assign_team_if_available(game.game_id, target.id, "BLUE"),
        db.assign_team_if_available(game.game_id, target.id, "RED"),
    )
    assert sorted(results) == [False, True]

    team_players = await db.get_game_players(game.game_id)
    picked = next(p for p in team_players if p.id == target.id)
    # Проверяем, что игрок оказался ровно в одной команде.
    blue = await db.get_team_players(game.game_id, "BLUE")
    red = await db.get_team_players(game.game_id, "RED")
    assert (target.id in [p.id for p in blue]) != (target.id in [p.id for p in red])


async def test_add_player_rejected_after_draft_transition(db):
    """BUG #3 regression: add_game_player_if_space must not add a player
    once the game already left WAITING, even if a late join races the
    WAITING->DRAFT transition."""
    players = await _make_players(db, 11, start_id=200)
    game = await db.create_game_if_none_active(guild_id=1)
    for p in players[:10]:
        await db.add_game_player_if_space(game.game_id, p.id, max_players=10)

    transitioned = await db.transition_game_status(game.game_id, "WAITING", "DRAFT")
    assert transitioned is True

    late = players[10]
    added = await db.add_game_player_if_space(game.game_id, late.id, max_players=10)
    assert added is False
    assert await db.is_player_in_game(game.game_id, late.id) is False


async def test_remove_player_rejected_after_draft_transition(db):
    """BUG #3 regression: remove_game_player must not silently pull a
    drafted player (or a captain) out of game_players once the game has
    left WAITING — a stray leave that raced the WAITING->DRAFT transition
    must be a no-op, not a state-corrupting delete."""
    players = await _make_players(db, 10, start_id=300)
    game = await db.create_game_if_none_active(guild_id=1)
    for p in players:
        await db.add_game_player_if_space(game.game_id, p.id, max_players=10)

    await db.transition_game_status(game.game_id, "WAITING", "DRAFT")
    await db.set_captains(game.game_id, players[0].id, players[1].id)

    removed = await db.remove_game_player(game.game_id, players[0].id)
    assert removed is False
    assert await db.is_player_in_game(game.game_id, players[0].id) is True

    game_row = await db.get_game(game.game_id)
    assert game_row.blue_captain == players[0].id


async def test_remove_player_allowed_while_waiting(db):
    """Sanity check that the BUG #3 CAS fix doesn't break a normal leave
    while the lobby is still open."""
    players = await _make_players(db, 3, start_id=400)
    game = await db.create_game_if_none_active(guild_id=1)
    for p in players:
        await db.add_game_player_if_space(game.game_id, p.id, max_players=10)

    removed = await db.remove_game_player(game.game_id, players[0].id)
    assert removed is True
    assert await db.is_player_in_game(game.game_id, players[0].id) is False


async def test_join_queue_blocks_active_game_player_atomically(db):
    """BUG #13 regression: join_queue must refuse a player who is already
    in an active (non-terminal) game for that guild, atomically."""
    players = await _make_players(db, 1, start_id=500)
    game = await db.create_game_if_none_active(guild_id=7)
    await db.add_game_player_if_space(game.game_id, players[0].id, max_players=10)

    added, total, position = await db.join_queue(7, players[0].id)
    assert added is False
    assert position == 0  # blocked by active game, not "already queued"


async def test_add_game_player_blocks_queued_player_atomically(db):
    """BUG #13 regression: a player sitting in the matchmaking queue must
    not also be addable to a /game lobby on the same guild."""
    players = await _make_players(db, 1, start_id=501)
    added, total, position = await db.join_queue(9, players[0].id)
    assert added is True

    game = await db.create_game_if_none_active(guild_id=9)
    joined = await db.add_game_player_if_space(game.game_id, players[0].id, max_players=10)
    assert joined is False
    assert await db.is_player_in_game(game.game_id, players[0].id) is False
