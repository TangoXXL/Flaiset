"""
Тесты очистки голосовых каналов — целевой тест на найденный и исправленный
критический баг: если удаление одного канала успешно, а второго завершается
ошибкой, БД не должна терять ссылку на второй (ещё существующий) канал.
"""

import pytest


async def _make_game_with_voice(db, guild_id=1, blue_id=111, red_id=222):
    game = await db.create_game_if_none_active(guild_id=guild_id)
    await db.set_voice_channels(game.game_id, blue_id, red_id)
    return game


async def test_clear_voice_channels_both(db):
    game = await _make_game_with_voice(db)
    await db.clear_voice_channels(game.game_id, clear_blue=True, clear_red=True)

    updated = await db.get_game(game.game_id)
    assert updated.blue_voice_channel_id is None
    assert updated.red_voice_channel_id is None


async def test_clear_voice_channels_partial_keeps_failed_one(db):
    """Ключевой тест исправления п.4 ревью: если удалился только синий
    канал (красный упал с ошибкой), в БД должен остаться ТОЛЬКО ID
    красного канала — иначе он "утечёт" в гильдии навсегда, так как
    cleanup после рестарта не найдёт игру с уже обнулённым ID."""
    game = await _make_game_with_voice(db, blue_id=111, red_id=222)

    await db.clear_voice_channels(game.game_id, clear_blue=True, clear_red=False)

    updated = await db.get_game(game.game_id)
    assert updated.blue_voice_channel_id is None
    assert updated.red_voice_channel_id == 222  # не потерян


async def test_clear_voice_channels_noop_when_nothing_resolved(db):
    """Если ни один канал не был подтверждённо обработан (оба упали
    с ошибкой), запись в БД вообще не должна вызываться."""
    game = await _make_game_with_voice(db, blue_id=111, red_id=222)

    await db.clear_voice_channels(game.game_id, clear_blue=False, clear_red=False)

    updated = await db.get_game(game.game_id)
    assert updated.blue_voice_channel_id == 111
    assert updated.red_voice_channel_id == 222


async def test_get_games_with_leftover_voice_channels(db):
    game = await _make_game_with_voice(db, blue_id=111, red_id=222)
    await db.update_game_status(game.game_id, "COMPLETED")

    leftover = await db.get_games_with_leftover_voice_channels()
    assert any(g.game_id == game.game_id for g in leftover)

    # После частичной очистки (только синий) игра всё ещё должна считаться
    # "с оставшимся каналом", потому что красный ID всё ещё в БД.
    await db.clear_voice_channels(game.game_id, clear_blue=True, clear_red=False)
    leftover_after = await db.get_games_with_leftover_voice_channels()
    game_after = next(g for g in leftover_after if g.game_id == game.game_id)
    assert game_after.blue_voice_channel_id is None
    assert game_after.red_voice_channel_id == 222

    # Полная очистка убирает игру из списка "оставшихся".
    await db.clear_voice_channels(game.game_id, clear_blue=True, clear_red=True)
    leftover_final = await db.get_games_with_leftover_voice_channels()
    assert not any(g.game_id == game.game_id for g in leftover_final)


async def test_cancel_active_game_atomic_blocks_after_completion(db):
    """/cancel_game не должен перезаписывать статус уже завершённой игры
    (например, если результат подтвердился ELO-транзакцией буквально
    мгновением раньше, чем сработала команда отмены)."""
    game = await db.create_game_if_none_active(guild_id=1)
    await db.update_game_status(game.game_id, "COMPLETED")

    cancelled = await db.cancel_active_game_atomic(game.game_id)
    assert cancelled is False

    updated = await db.get_game(game.game_id)
    assert updated.status == "COMPLETED"  # не перезаписан на CANCELLED


async def test_cancel_active_game_atomic_succeeds_while_active(db):
    game = await db.create_game_if_none_active(guild_id=1)

    cancelled = await db.cancel_active_game_atomic(game.game_id)
    assert cancelled is True

    updated = await db.get_game(game.game_id)
    assert updated.status == "CANCELLED"
