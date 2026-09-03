"""
Тесты подтверждения результата, начисления ELO, повторного подтверждения,
исправления результата (/edit_result) и отката при ошибках.
"""

import asyncio

import pytest


async def test_write_lock_prevents_commit_inside_result_transaction(db, monkeypatch):
    """A second writer must not commit halfway through ELO finalization.

    This used to be possible because aiosqlite queues individual execute()
    calls, while a transaction contains several awaits.  The injected writer
    runs while the finalizer is between statements; with the Database write
    lock it can only complete after the finalizer has committed.
    """
    game = await db.create_game_if_none_active(guild_id=901)
    await db.update_game_status(game.game_id, "PENDING_CONFIRMATION")
    users = [
        await db.create_user_atomic(9000 + i, f"P{i}", str(9000 + i), "1")
        for i in range(2)
    ]
    for user in users:
        await db.add_game_player(game.game_id, user.id)

    original = db._apply_result_statements
    writer_started = asyncio.Event()
    writer_task = None

    async def interleaving_statement(*args, **kwargs):
        nonlocal writer_task
        if not writer_started.is_set():
            writer_started.set()
            writer_task = asyncio.create_task(db.sync_username(users[0].discord_id, "updated"))
            await asyncio.sleep(0)
            assert not writer_task.done(), "an unrelated writer committed inside the active transaction"
        return await original(*args, **kwargs)

    monkeypatch.setattr(db, "_apply_result_statements", interleaving_statement)
    completed = await db.finalize_game_result_atomic(
        game.game_id, "BLUE", 1, [users[0].id], [users[1].id], 25, -25
    )

    assert completed is True
    assert writer_task is not None
    await writer_task

import config


async def _setup_full_game(db):
    """Создаёт игру с 10 игроками, назначенными в команды (5 BLUE / 5 RED),
    в статусе PENDING_CONFIRMATION — готовую к подтверждению результата."""
    game = await db.create_game_if_none_active(guild_id=1)
    players = []
    for i in range(1, 11):
        u = await db.create_user_atomic(i, f"Player{i}", str(2000 + i), "1")
        players.append(u)
        await db.add_game_player_if_space(game.game_id, u.id, max_players=10)

    blue, red = players[:5], players[5:]
    await db.set_captains(game.game_id, blue[0].id, red[0].id)
    for p in blue[1:]:
        await db.assign_team_if_available(game.game_id, p.id, "BLUE")
    for p in red[1:]:
        await db.assign_team_if_available(game.game_id, p.id, "RED")

    await db.transition_game_status(game.game_id, "WAITING", "DRAFT")
    await db.transition_game_status(game.game_id, "DRAFT", "PLAYING")
    await db.submit_screenshot_atomic(game.game_id, message_id=999)

    return game, blue, red


async def test_elo_calculation_win_loss(db):
    game, blue, red = await _setup_full_game(db)

    ok = await db.finalize_game_result_atomic(
        game.game_id,
        winner="BLUE",
        confirmed_by=1,
        winner_ids=[p.id for p in blue],
        loser_ids=[p.id for p in red],
        elo_win=config.ELO_WIN,
        elo_loss=config.ELO_LOSS,
        min_elo=config.MIN_ELO,
    )
    assert ok is True

    for p in blue:
        u = await db.get_user_by_id(p.id)
        assert u.elo == 125
        assert u.wins == 1
        assert u.losses == 0
        assert u.games_played == 1

    for p in red:
        u = await db.get_user_by_id(p.id)
        assert u.elo == 75
        assert u.wins == 0
        assert u.losses == 1


async def test_elo_clamped_at_min(db):
    """ELO не должен уходить ниже MIN_ELO (по умолчанию 0)."""
    game, blue, red = await _setup_full_game(db)

    # Загоняем всех "красных" почти в ноль перед поражением.
    for p in red:
        await db.get_user_by_id(p.id)
    for p in red:
        u = await db.get_user_by_id(p.id)
        # напрямую двигаем ELO вниз, как будто уже было много поражений
        await db._conn.execute("UPDATE users SET elo = 10 WHERE id = ?", (p.id,))
    await db._conn.commit()

    ok = await db.finalize_game_result_atomic(
        game.game_id,
        winner="BLUE",
        confirmed_by=1,
        winner_ids=[p.id for p in blue],
        loser_ids=[p.id for p in red],
        elo_win=config.ELO_WIN,
        elo_loss=config.ELO_LOSS,  # -25
        min_elo=config.MIN_ELO,  # 0
    )
    assert ok is True

    for p in red:
        u = await db.get_user_by_id(p.id)
        assert u.elo == 0  # не -15
        history = await db._conn.execute_fetchall(
            "SELECT elo_change FROM elo_history WHERE player_id = ? AND game_id = ?",
            (p.id, game.game_id),
        )
        # Фактически применённое изменение (-10), а не «сырое» -25, должно
        # быть записано в историю — иначе история будет лгать.
        assert history[0][0] == -10


async def test_double_confirmation_rejected(db):
    """Повторное подтверждение результата не должно начислять ELO дважды."""
    game, blue, red = await _setup_full_game(db)

    kwargs = dict(
        winner_ids=[p.id for p in blue],
        loser_ids=[p.id for p in red],
        elo_win=config.ELO_WIN,
        elo_loss=config.ELO_LOSS,
        min_elo=config.MIN_ELO,
    )
    first = await db.finalize_game_result_atomic(game.game_id, "BLUE", confirmed_by=1, **kwargs)
    second = await db.finalize_game_result_atomic(game.game_id, "BLUE", confirmed_by=2, **kwargs)

    assert first is True
    assert second is False

    for p in blue:
        u = await db.get_user_by_id(p.id)
        assert u.elo == 125  # не 150 — второе подтверждение не применилось


async def test_concurrent_confirmation_only_one_applies(db):
    """Два администратора почти одновременно жмут разные кнопки
    (BLUE/RED) или одну и ту же — ELO должен начислиться ровно один раз."""
    game, blue, red = await _setup_full_game(db)

    kwargs_common = dict(
        winner_ids=[p.id for p in blue],
        loser_ids=[p.id for p in red],
        elo_win=config.ELO_WIN,
        elo_loss=config.ELO_LOSS,
        min_elo=config.MIN_ELO,
    )
    results = await asyncio.gather(
        db.finalize_game_result_atomic(game.game_id, "BLUE", confirmed_by=1, **kwargs_common),
        db.finalize_game_result_atomic(game.game_id, "BLUE", confirmed_by=2, **kwargs_common),
    )
    assert sorted(results) == [False, True]

    total_wins = 0
    for p in blue:
        u = await db.get_user_by_id(p.id)
        total_wins += u.wins
    assert total_wins == 5  # каждый выиграл ровно один раз, не два


async def test_edit_result_rollback_and_reapply(db):
    """/edit_result: откат старого начисления + начисление по новому
    победителю должны быть согласованы (ELO не «убегает» и не теряется)."""
    game, blue, red = await _setup_full_game(db)
    await db.finalize_game_result_atomic(
        game.game_id, "BLUE", confirmed_by=1,
        winner_ids=[p.id for p in blue], loser_ids=[p.id for p in red],
        elo_win=config.ELO_WIN, elo_loss=config.ELO_LOSS, min_elo=config.MIN_ELO,
    )

    ok = await db.edit_game_result_atomic(
        game.game_id,
        expected_current_winner="BLUE",
        new_winner="RED",
        confirmed_by=1,
        new_winner_ids=[p.id for p in red],
        new_loser_ids=[p.id for p in blue],
        elo_win=config.ELO_WIN,
        elo_loss=config.ELO_LOSS,
        min_elo=config.MIN_ELO,
    )
    assert ok is True

    for p in red:
        u = await db.get_user_by_id(p.id)
        assert u.elo == 125
        assert u.wins == 1
        assert u.losses == 0
        assert u.games_played == 1  # не 2 — откат старого результата сработал

    for p in blue:
        u = await db.get_user_by_id(p.id)
        assert u.elo == 75
        assert u.wins == 0
        assert u.losses == 1
        assert u.games_played == 1

    game_after = await db.get_game(game.game_id)
    assert game_after.winner == "RED"


async def test_edit_result_race_rejected(db):
    """Если победитель успел измениться между чтением и вызовом
    edit_game_result_atomic (двойной /edit_result), второй вызов должен
    вернуть False и не трогать ELO повторно."""
    game, blue, red = await _setup_full_game(db)
    await db.finalize_game_result_atomic(
        game.game_id, "BLUE", confirmed_by=1,
        winner_ids=[p.id for p in blue], loser_ids=[p.id for p in red],
        elo_win=config.ELO_WIN, elo_loss=config.ELO_LOSS, min_elo=config.MIN_ELO,
    )

    # Первый вызов "видел" победителя BLUE и меняет его на RED.
    ok1 = await db.edit_game_result_atomic(
        game.game_id, expected_current_winner="BLUE", new_winner="RED", confirmed_by=1,
        new_winner_ids=[p.id for p in red], new_loser_ids=[p.id for p in blue],
        elo_win=config.ELO_WIN, elo_loss=config.ELO_LOSS, min_elo=config.MIN_ELO,
    )
    # Второй вызов тоже "думает", что текущий победитель ещё BLUE (устаревшее
    # состояние) — не должен применяться, раз RED уже был закреплён первым.
    ok2 = await db.edit_game_result_atomic(
        game.game_id, expected_current_winner="BLUE", new_winner="RED", confirmed_by=2,
        new_winner_ids=[p.id for p in red], new_loser_ids=[p.id for p in blue],
        elo_win=config.ELO_WIN, elo_loss=config.ELO_LOSS, min_elo=config.MIN_ELO,
    )
    assert ok1 is True
    assert ok2 is False

    for p in red:
        u = await db.get_user_by_id(p.id)
        assert u.wins == 1  # не 2 — второй (устаревший) edit не применился


async def test_finalize_rollback_on_bad_composition(db):
    """Если среди id победителей окажется несуществующий игрок, вся
    транзакция должна откатиться — никто не должен получить ELO частично."""
    game, blue, red = await _setup_full_game(db)

    bad_winner_ids = [p.id for p in blue] + [999999]  # несуществующий player_id

    with pytest.raises(AssertionError):
        await db.finalize_game_result_atomic(
            game.game_id, "BLUE", confirmed_by=1,
            winner_ids=bad_winner_ids, loser_ids=[p.id for p in red],
            elo_win=config.ELO_WIN, elo_loss=config.ELO_LOSS, min_elo=config.MIN_ELO,
        )

    # Откат должен быть полным: даже игроки, обработанные ДО "плохого" id,
    # не должны сохранить изменения.
    for p in blue:
        u = await db.get_user_by_id(p.id)
        assert u.elo == 100
        assert u.wins == 0
        assert u.games_played == 0

    game_after = await db.get_game(game.game_id)
    assert game_after.status == "PENDING_CONFIRMATION"
    assert game_after.winner is None
