"""Regression tests for the final production-hardening pass.

Covers:
* shop per-user lock cleanup (no leak, concurrent-safe)
* concurrent shop purchases serialized per user
* result embed 🛡️ only when protection actually fired this match
* balance_teams driven by config.PLAYERS_PER_GAME (not magic 10)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import config
from database.models import User
from services.matchmaking import balance_teams
from utils.helpers import _result_lines, build_result_announcement_embed


def _make_user(
    uid: int,
    *,
    elo: int = 100,
    username: str | None = None,
    elo_protection: int = 0,
) -> User:
    return User(
        id=uid,
        discord_id=1000 + uid,
        discord_username=username or f"player{uid}",
        mlbb_id=None,
        server_id=None,
        mlbb_nickname=None,
        verified=True,
        elo=elo,
        wins=0,
        losses=0,
        games_played=0,
        created_at="2026-01-01T00:00:00+00:00",
        vc_balance=0,
        elo_protection=elo_protection,
    )


# ---------------------------------------------------------------------------
# Shop lock cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shop_lock_cleaned_after_use():
    """After the context exits, the per-user lock entry must be gone."""
    from cogs.shop import Shop

    cog = Shop(bot=MagicMock())
    assert cog._locks == {}

    async with cog._user_lock(42):
        assert 42 in cog._locks
        lock, refs = cog._locks[42]
        assert refs == 1
        assert lock.locked()

    assert cog._locks == {}


@pytest.mark.asyncio
async def test_shop_lock_no_leak_after_many_users():
    """Many sequential users must not accumulate lock entries."""
    from cogs.shop import Shop

    cog = Shop(bot=MagicMock())
    for uid in range(50):
        async with cog._user_lock(uid):
            pass
    assert cog._locks == {}


@pytest.mark.asyncio
async def test_shop_lock_serializes_concurrent_holders():
    """Two concurrent acquisitions for the same user run one after another."""
    from cogs.shop import Shop

    cog = Shop(bot=MagicMock())
    order: list[str] = []

    async def worker(name: str) -> None:
        async with cog._user_lock(7):
            order.append(f"{name}-enter")
            await asyncio.sleep(0.05)
            order.append(f"{name}-exit")

    await asyncio.gather(worker("a"), worker("b"))
    # Strict serialization: one fully completes before the other starts work
    assert order in (
        ["a-enter", "a-exit", "b-enter", "b-exit"],
        ["b-enter", "b-exit", "a-enter", "a-exit"],
    )
    assert cog._locks == {}


@pytest.mark.asyncio
async def test_shop_lock_refcount_under_contention():
    """Waiters keep the entry alive until the last one leaves; no double-delete race."""
    from cogs.shop import Shop

    cog = Shop(bot=MagicMock())
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with cog._user_lock(1):
            started.set()
            await release.wait()

    async def waiter() -> None:
        await started.wait()
        async with cog._user_lock(1):
            pass

    t1 = asyncio.create_task(holder())
    t2 = asyncio.create_task(waiter())
    await started.wait()
    # Holder is inside; waiter is blocked on the per-user lock (refcount >= 2)
    assert 1 in cog._locks
    assert cog._locks[1][1] >= 2
    release.set()
    await asyncio.gather(t1, t2)
    assert cog._locks == {}


# ---------------------------------------------------------------------------
# Casino lock (same pattern)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_casino_lock_cleaned_after_use():
    from cogs.casino import Casino

    cog = Casino(bot=MagicMock())
    async with cog._user_lock(99):
        assert 99 in cog._locks
    assert cog._locks == {}


# ---------------------------------------------------------------------------
# 🛡️ only when protection actually fired
# ---------------------------------------------------------------------------


def test_result_lines_no_shield_when_protection_not_used():
    """Player still owns protection items, but none fired this match → no 🛡️."""
    user = _make_user(1, username="Tank", elo_protection=3)
    lines = _result_lines([(user, -25, False)])
    assert "🛡️" not in lines
    assert "Tank" in lines
    assert "-25" in lines


def test_result_lines_shield_when_protection_used():
    """Protection was consumed this match → 🛡️ shown regardless of inventory."""
    user = _make_user(2, username="Protected", elo_protection=0)
    lines = _result_lines([(user, 0, True)])
    assert "🛡️" in lines
    assert "Protected" in lines


def test_result_announcement_embed_uses_protection_used_flag():
    loser = _make_user(1, username="Loser", elo_protection=5)
    winner = _make_user(2, username="Winner", elo_protection=0)
    details = {
        "BLUE": [(winner, 25, False)],
        "RED": [(loser, 0, True)],
    }
    embed = build_result_announcement_embed(42, "BLUE", details)
    # Field values are the rendered lines
    blue_val = embed.fields[0].value
    red_val = embed.fields[1].value
    assert "🛡️" not in blue_val
    assert "🛡️" in red_val


@pytest.mark.asyncio
async def test_get_game_result_details_exposes_protection_used(db):
    """End-to-end: finalize a loss with protection → details flag is True."""
    game = await db.create_game_if_none_active(guild_id=9101)
    users = []
    for i in range(10):
        u = await db.create_user_atomic(9100 + i, f"u{i}", str(9100 + i), "1")
        users.append(u)
        await db.add_game_player_if_space(game.game_id, u.id, 10)

    blue, red = users[:5], users[5:]
    await db.set_captains(game.game_id, blue[0].id, red[0].id)
    for p in blue[1:]:
        await db.assign_team_if_available(game.game_id, p.id, "BLUE")
    for p in red[1:]:
        await db.assign_team_if_available(game.game_id, p.id, "RED")
    await db.transition_game_status(game.game_id, "WAITING", "DRAFT")
    await db.transition_game_status(game.game_id, "DRAFT", "PLAYING")
    await db.submit_screenshot_atomic(game.game_id, 1)

    # Give one loser protection
    await db.credit_vc(red[0].id, config.ELO_PROTECTION_PRICE, "ADMIN_GRANT", "seed")
    await db.purchase_elo_protection(red[0].id, config.ELO_PROTECTION_PRICE)

    win_map = {p.id: 50 for p in blue}
    loss_map = {p.id: 10 for p in red}
    ok = await db.finalize_game_result_atomic(
        game.game_id,
        "BLUE",
        1,
        [p.id for p in blue],
        [p.id for p in red],
        25,
        -25,
        min_elo=0,
        win_vc_map=win_map,
        loss_vc_map=loss_map,
    )
    assert ok

    details = await db.get_game_result_details(game.game_id)
    # Find red[0] in details
    red_entries = {u.id: (u, change, used) for u, change, used in details["RED"]}
    assert red[0].id in red_entries
    _, change, used = red_entries[red[0].id]
    assert used is True
    assert change == 0  # protection blocked the -25

    # Other losers: no protection
    for p in red[1:]:
        _, ch, used2 = red_entries[p.id]
        assert used2 is False
        assert ch == -25

    # Winners never use protection
    for u, change, used in details["BLUE"]:
        assert used is False
        assert change == 25

    # Inventory still shows remaining protections correctly after consume
    refreshed = await db.get_user_by_id(red[0].id)
    assert refreshed.elo_protection == 0

    # Embed must show 🛡️ only for the protected loser
    lines_red = _result_lines(details["RED"])
    assert "🛡️" in lines_red
    # Exactly one shield (the protected player)
    assert lines_red.count("🛡️") == 1


# ---------------------------------------------------------------------------
# balance_teams / PLAYERS_PER_GAME
# ---------------------------------------------------------------------------


def test_balance_teams_default_10():
    players = [_make_user(i, elo=100 + i * 10) for i in range(10)]
    blue, red = balance_teams(players)
    assert len(blue) == 5
    assert len(red) == 5
    assert {p.id for p in blue + red} == {p.id for p in players}


def test_balance_teams_rejects_wrong_count():
    players = [_make_user(i) for i in range(8)]
    with pytest.raises(ValueError, match="exactly"):
        balance_teams(players)


def test_balance_teams_respects_config_size(monkeypatch):
    """Algorithm must work for an alternate even size (e.g. 6 → 3v3)."""
    monkeypatch.setattr(config, "PLAYERS_PER_GAME", 6)
    players = [
        _make_user(1, elo=100),
        _make_user(2, elo=200),
        _make_user(3, elo=150),
        _make_user(4, elo=180),
        _make_user(5, elo=120),
        _make_user(6, elo=160),
    ]
    blue, red = balance_teams(players)
    assert len(blue) == 3
    assert len(red) == 3
    assert {p.id for p in blue + red} == {1, 2, 3, 4, 5, 6}
    # Deterministic: same input → same teams
    blue2, red2 = balance_teams(players)
    assert [p.id for p in blue] == [p.id for p in blue2]
    assert [p.id for p in red] == [p.id for p in red2]


def test_balance_teams_rejects_odd_configured_size(monkeypatch):
    monkeypatch.setattr(config, "PLAYERS_PER_GAME", 9)
    players = [_make_user(i) for i in range(9)]
    with pytest.raises(ValueError, match="even"):
        balance_teams(players)


def test_balance_teams_minimizes_elo_diff():
    # 4 high + 4 low would be unbalanced; mixed is better
    players = [
        _make_user(1, elo=1000),
        _make_user(2, elo=900),
        _make_user(3, elo=100),
        _make_user(4, elo=110),
        _make_user(5, elo=120),
        _make_user(6, elo=130),
        _make_user(7, elo=140),
        _make_user(8, elo=150),
        _make_user(9, elo=160),
        _make_user(10, elo=170),
    ]
    blue, red = balance_teams(players)
    blue_sum = sum(p.elo for p in blue)
    red_sum = sum(p.elo for p in red)
    # Optimal split should keep teams reasonably close
    assert abs(blue_sum - red_sum) < 500
