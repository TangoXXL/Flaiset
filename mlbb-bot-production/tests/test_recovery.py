"""
Тесты восстановления состояния после рестарта бота (Этап 39) — на уровне
данных, которые читает recover_games()/recover_pending_confirmations() из
database.py. Сами discord.py Views здесь не тестируются (для этого нужен
живой Discord Gateway), но корректность запросов, на которых строится
восстановление, полностью покрыта.
"""

import pytest


async def test_get_games_by_statuses_finds_waiting_and_draft(db):
    g1 = await db.create_game_if_none_active(guild_id=1)
    await db.update_game_status(g1.game_id, "COMPLETED")

    g2 = await db.create_game_if_none_active(guild_id=2)  # остаётся WAITING

    g3 = await db.create_game_if_none_active(guild_id=3)
    await db.transition_game_status(g3.game_id, "WAITING", "DRAFT")

    found = await db.get_games_by_statuses(("WAITING", "DRAFT"))
    found_ids = {g.game_id for g in found}
    assert g2.game_id in found_ids
    assert g3.game_id in found_ids
    assert g1.game_id not in found_ids


async def test_lobby_and_draft_message_ids_persisted(db):
    """recover_games() полагается на сохранённые lobby/draft message id —
    проверяем, что они действительно сохраняются и читаются обратно."""
    game = await db.create_game_if_none_active(guild_id=1)
    await db.set_lobby_message(game.game_id, channel_id=555, message_id=777)
    await db.set_draft_message(game.game_id, message_id=888)

    reloaded = await db.get_game(game.game_id)
    assert reloaded.lobby_channel_id == 555
    assert reloaded.lobby_message_id == 777
    assert reloaded.draft_message_id == 888


async def test_recover_draft_pick_index_matches_assigned_players(db):
    """_recover_draft в cogs/game.py вычисляет pick_index как количество
    уже выбранных НЕ-капитанов — проверяем, что get_team_players отдаёт
    именно то, что нужно для этого вычисления."""
    game = await db.create_game_if_none_active(guild_id=1)
    players = []
    for i in range(1, 11):
        u = await db.create_user_atomic(i, f"P{i}", str(3000 + i), "1")
        players.append(u)
        await db.add_game_player_if_space(game.game_id, u.id, max_players=10)

    blue_cap, red_cap = players[0], players[1]
    await db.set_captains(game.game_id, blue_cap.id, red_cap.id)
    await db.transition_game_status(game.game_id, "WAITING", "DRAFT")

    # Капитан синих успел выбрать одного игрока до рестарта.
    await db.assign_team_if_available(game.game_id, players[2].id, "BLUE")

    blue_team_all = await db.get_team_players(game.game_id, "BLUE")
    red_team_all = await db.get_team_players(game.game_id, "RED")
    blue_team = [p for p in blue_team_all if p.id != blue_cap.id]
    red_team = [p for p in red_team_all if p.id != red_cap.id]

    assert len(blue_team) == 1
    assert len(red_team) == 0
    pick_index = len(blue_team) + len(red_team)
    assert pick_index == 1

    available = await db.get_available_players(game.game_id)
    assert len(available) == 10 - 2 - 1  # минус 2 капитана, минус 1 выбранный


async def test_recover_pending_confirmation_games_found(db):
    game = await db.create_game_if_none_active(guild_id=1)
    await db.transition_game_status(game.game_id, "WAITING", "DRAFT")
    await db.transition_game_status(game.game_id, "DRAFT", "PLAYING")
    await db.submit_screenshot_atomic(game.game_id, message_id=42)

    pending = await db.get_games_by_statuses(("PENDING_CONFIRMATION",))
    assert any(g.game_id == game.game_id for g in pending)
    found = next(g for g in pending if g.game_id == game.game_id)
    assert found.screenshot_message_id == 42


async def test_database_reconnect_preserves_data(tmp_path):
    """Имитация настоящего рестарта процесса: закрываем соединение и
    открываем новое к тому же файлу — все данные должны сохраниться."""
    from database.database import Database

    path = str(tmp_path / "restart_test.db")

    db1 = Database(path)
    await db1.connect()
    user = await db1.create_user_atomic(1, "Alice", "111", "1")
    game = await db1.create_game_if_none_active(guild_id=1)
    await db1.close()

    db2 = Database(path)
    await db2.connect()
    try:
        reloaded_user = await db2.get_user_by_discord_id(1)
        reloaded_game = await db2.get_game(game.game_id)
        assert reloaded_user is not None
        assert reloaded_user.mlbb_id == "111"
        assert reloaded_game is not None
        assert reloaded_game.status == "WAITING"
    finally:
        await db2.close()


async def test_cancel_and_requeue_atomic_returns_players(db):
    """Fix (аудит, критический — п.2/п.11): если матч из очереди не удалось
    анонсировать после claim_queue_match, cancel_and_requeue_atomic должен
    одной транзакцией отменить игру и вернуть всех игроков в очередь, а не
    оставить игру висеть или потерять игроков."""
    guild_id = 500
    players = []
    for i in range(1, 11):
        u = await db.create_user_atomic(5000 + i, f"Q{i}", str(6000 + i), "1")
        players.append(u)
        added, total, position = await db.join_queue(guild_id, u.id)
        assert added

    game, claimed = await db.claim_queue_match(guild_id, max_players=10)
    assert game is not None
    assert len(claimed) == 10

    # Очередь пуста сразу после claim.
    entries = await db.get_queue_entries(guild_id)
    assert entries == []

    # Объявление "не удалось отправить" -> откатываем.
    cancelled = await db.cancel_and_requeue_atomic(
        game.game_id, guild_id, [p.id for p in claimed]
    )
    assert cancelled is True

    reloaded_game = await db.get_game(game.game_id)
    assert reloaded_game.status == "CANCELLED"

    entries_after = await db.get_queue_entries(guild_id)
    assert {e.player.id for e in entries_after} == {p.id for p in claimed}

    # На сервере больше нет активной игры -> claim/create снова возможны.
    assert await db.get_active_game(guild_id) is None


async def test_cancel_and_requeue_atomic_noop_when_already_terminal(db):
    """Если игра уже COMPLETED/CANCELLED (гонка с другим путём завершения),
    cancel_and_requeue_atomic не должен перезаписывать статус и не должен
    возвращать игроков в очередь (rowcount==0 -> ничего не делаем)."""
    game = await db.create_game_if_none_active(guild_id=501)
    u = await db.create_user_atomic(5100, "QQ", "6100", "1")
    await db.update_game_status(game.game_id, "COMPLETED")

    result = await db.cancel_and_requeue_atomic(game.game_id, 501, [u.id])
    assert result is False

    reloaded = await db.get_game(game.game_id)
    assert reloaded.status == "COMPLETED"
    entries = await db.get_queue_entries(501)
    assert entries == []  # not requeued, since the cancel itself was a no-op


async def test_recover_games_cancels_orphan_waiting_without_channel(db):
    """Fix (аудит, критический — 'зависшие состояния', п.11): WAITING-игра
    без lobby_channel_id (crash между create_game_if_none_active и
    set_lobby_message) больше не должна навсегда блокировать создание
    новых игр на сервере — recover_games() должна её автоматически отменить.
    Этот тест бьёт по той же логике на уровне БД, которую использует
    cogs/game.py.recover_games() (сам discord.py View здесь не поднимается)."""
    guild_id = 502
    game = await db.create_game_if_none_active(guild_id)
    assert game.lobby_channel_id is None  # ещё не анонсирована

    # Имитация той же ветки, которую теперь выполняет recover_games():
    players = await db.get_game_players(game.game_id)
    assert players == []
    cancelled = await db.cancel_active_game_atomic(game.game_id)
    assert cancelled is True

    assert await db.get_active_game(guild_id) is None
    # Новый /game теперь снова возможен.
    new_game = await db.create_game_if_none_active(guild_id)
    assert new_game is not None
    assert new_game.game_id != game.game_id


async def test_recover_games_requeues_orphan_draft_from_queue(db):
    """То же самое, но для DRAFT-игры, собранной из очереди (claim_queue_match) —
    у неё уже есть 10 назначенных игроков, и при отмене их нужно вернуть в
    очередь, а не просто отменить игру и потерять состав."""
    guild_id = 503
    players = []
    for i in range(1, 11):
        u = await db.create_user_atomic(5200 + i, f"R{i}", str(6200 + i), "1")
        players.append(u)
        await db.join_queue(guild_id, u.id)

    game, claimed = await db.claim_queue_match(guild_id, max_players=10)
    await db.transition_game_status(game.game_id, "WAITING", "DRAFT")
    assert game.lobby_channel_id is None  # объявление ещё не отправлено/сохранено

    reloaded = await db.get_game(game.game_id)
    assert reloaded.lobby_channel_id is None

    # Эквивалент ветки recover_games() для игр без lobby_channel_id, у которых
    # уже есть игроки — используем cancel_and_requeue_atomic вместо простого cancel.
    db_players = await db.get_game_players(game.game_id)
    assert len(db_players) == 10
    result = await db.cancel_and_requeue_atomic(
        game.game_id, guild_id, [p.id for p in db_players]
    )
    assert result is True

    entries = await db.get_queue_entries(guild_id)
    assert len(entries) == 10
    assert await db.get_active_game(guild_id) is None
