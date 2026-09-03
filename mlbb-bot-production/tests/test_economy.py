"""Тесты Valhalla Coin, Elo Protection, shop и casino."""

import pytest

import config
from services.casino import spin_slots
from services.economy import roll_match_vc


async def _user(db, i: int):
    return await db.create_user_atomic(10_000 + i, f"Eco{i}", str(10_000 + i), "1")


async def test_roll_match_vc_ranges():
    for _ in range(50):
        w = roll_match_vc(True)
        l = roll_match_vc(False)
        assert config.WIN_COINS_MIN <= w <= config.WIN_COINS_MAX
        assert config.LOSS_COINS_MIN <= l <= config.LOSS_COINS_MAX


async def test_credit_debit_and_no_negative(db):
    u = await _user(db, 1)
    bal = await db.credit_vc(u.id, 100, "ADMIN_GRANT", "test")
    assert bal == 100
    bal = await db.debit_vc(u.id, 40, "SHOP_PURCHASE", "test")
    assert bal == 60
    with pytest.raises(ValueError):
        await db.debit_vc(u.id, 1000, "SHOP_PURCHASE", "too much")
    u2 = await db.get_user_by_id(u.id)
    assert u2.vc_balance == 60


async def test_match_rewards_once(db):
    game = await db.create_game_if_none_active(guild_id=77)
    users = [await _user(db, i) for i in range(10)]
    for u in users:
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

    win_map = {p.id: 70 for p in blue}
    loss_map = {p.id: 20 for p in red}
    ok = await db.finalize_game_result_atomic(
        game.game_id, "BLUE", 1,
        [p.id for p in blue], [p.id for p in red],
        25, -25, 0, win_map, loss_map,
    )
    assert ok is True
    u = await db.get_user_by_id(blue[0].id)
    assert u.vc_balance == 70
    # second finalize must not pay again
    ok2 = await db.finalize_game_result_atomic(
        game.game_id, "BLUE", 1,
        [p.id for p in blue], [p.id for p in red],
        25, -25, 0, win_map, loss_map,
    )
    assert ok2 is False
    u = await db.get_user_by_id(blue[0].id)
    assert u.vc_balance == 70


async def test_elo_protection_on_loss(db):
    game = await db.create_game_if_none_active(guild_id=78)
    users = [await _user(db, 100 + i) for i in range(10)]
    for u in users:
        await db.add_game_player_if_space(game.game_id, u.id, 10)
    blue, red = users[:5], users[5:]
    await db.set_captains(game.game_id, blue[0].id, red[0].id)
    for p in blue[1:]:
        await db.assign_team_if_available(game.game_id, p.id, "BLUE")
    for p in red[1:]:
        await db.assign_team_if_available(game.game_id, p.id, "RED")

    # give protection + some elo to a loser
    await db.credit_vc(red[1].id, 2000, "ADMIN_GRANT", "seed")
    await db.purchase_elo_protection(red[1].id, config.ELO_PROTECTION_PRICE)
    await db.admin_set_elo(red[1].id, 200, 1, "seed")
    loser = await db.get_user_by_id(red[1].id)
    assert loser.elo_protection == 1
    assert loser.elo == 200

    await db.transition_game_status(game.game_id, "WAITING", "DRAFT")
    await db.transition_game_status(game.game_id, "DRAFT", "PLAYING")
    await db.submit_screenshot_atomic(game.game_id, 2)

    ok = await db.finalize_game_result_atomic(
        game.game_id, "BLUE", 1,
        [p.id for p in blue], [p.id for p in red],
        25, -25, 0,
        {p.id: 50 for p in blue},
        {p.id: 10 for p in red},
    )
    assert ok
    loser = await db.get_user_by_id(red[1].id)
    assert loser.elo == 200  # protected
    assert loser.elo_protection == 0
    assert loser.vc_balance == 2000 - config.ELO_PROTECTION_PRICE + 10


async def test_shop_insufficient(db):
    u = await _user(db, 50)
    with pytest.raises(ValueError):
        await db.purchase_elo_protection(u.id, config.ELO_PROTECTION_PRICE)


async def test_casino_rejects_overbet(db):
    u = await _user(db, 60)
    await db.credit_vc(u.id, 50, "ADMIN_GRANT", "seed")
    with pytest.raises(ValueError):
        await db.play_casino_slots(u.id, 100, ("🍒", "🍒", "🍒"), 0, "LOSS")


async def test_casino_spin_valid():
    for bet in config.CASINO_ALLOWED_BETS:
        spin = spin_slots(bet)
        assert len(spin.symbols) == 3
        assert spin.bet == bet
        assert spin.payout >= 0


async def test_casino_loss_reels_never_look_like_a_win(db):
    """BUG #8 regression: a LOSS spin must show three distinct symbols.
    Previously the fallback path only re-rolled when all three matched (a
    triple), so a LOSS could still render as e.g. two matching cherries —
    visually a "pair" win — even though the ledger paid out 0."""
    from services.casino import _symbols_for

    for _ in range(500):
        a, b, c = _symbols_for("LOSS")
        assert len({a, b, c}) == 3, (a, b, c)


async def test_admin_set_elo(db):
    u = await _user(db, 70)
    old, new = await db.admin_set_elo(u.id, 250, 999, "test")
    assert old == config.STARTING_ELO or old == 100
    assert new == 250
    u2 = await db.get_user_by_id(u.id)
    assert u2.elo == 250


async def test_edit_result_reverses_vc_and_protection(db):
    """После /edit_result VC и Elo Protection должны соответствовать новому победителю."""
    game = await db.create_game_if_none_active(guild_id=88)
    users = [await _user(db, 200 + i) for i in range(10)]
    for u in users:
        await db.add_game_player_if_space(game.game_id, u.id, 10)
    blue, red = users[:5], users[5:]
    await db.set_captains(game.game_id, blue[0].id, red[0].id)
    for p in blue[1:]:
        await db.assign_team_if_available(game.game_id, p.id, "BLUE")
    for p in red[1:]:
        await db.assign_team_if_available(game.game_id, p.id, "RED")

    # protection on a red player who will "lose" first, then become winner
    await db.credit_vc(red[0].id, 2000, "ADMIN_GRANT", "seed")
    await db.purchase_elo_protection(red[0].id, config.ELO_PROTECTION_PRICE)
    await db.admin_set_elo(red[0].id, 300, 1, "seed")

    await db.transition_game_status(game.game_id, "WAITING", "DRAFT")
    await db.transition_game_status(game.game_id, "DRAFT", "PLAYING")
    await db.submit_screenshot_atomic(game.game_id, 3)

    win_map = {p.id: 60 for p in blue}
    loss_map = {p.id: 15 for p in red}
    ok = await db.finalize_game_result_atomic(
        game.game_id, "BLUE", 1,
        [p.id for p in blue], [p.id for p in red],
        25, -25, 0, win_map, loss_map,
    )
    assert ok
    protected = await db.get_user_by_id(red[0].id)
    assert protected.elo_protection == 0
    assert protected.elo == 300  # protected loss
    assert protected.vc_balance == 2000 - config.ELO_PROTECTION_PRICE + 15

    blue_player = await db.get_user_by_id(blue[0].id)
    assert blue_player.vc_balance == 60

    # Flip winner to RED
    new_win = {p.id: 55 for p in red}
    new_loss = {p.id: 12 for p in blue}
    edited = await db.edit_game_result_atomic(
        game.game_id,
        expected_current_winner="BLUE",
        new_winner="RED",
        confirmed_by=1,
        new_winner_ids=[p.id for p in red],
        new_loser_ids=[p.id for p in blue],
        elo_win=25,
        elo_loss=-25,
        min_elo=0,
        win_vc_map=new_win,
        loss_vc_map=new_loss,
    )
    assert edited is True

    red0 = await db.get_user_by_id(red[0].id)
    # protection restored then not consumed (now a win)
    assert red0.elo_protection == 1
    assert red0.vc_balance == 2000 - config.ELO_PROTECTION_PRICE + 55
    assert red0.elo == 300 + 25

    blue0 = await db.get_user_by_id(blue[0].id)
    # original +60 VC reversed, +12 loss VC
    assert blue0.vc_balance == 12


async def test_edit_result_does_not_go_negative_after_spend(db):
    """BUG #1 regression: if a player already spent their match VC before
    an admin flips the winner, the revert in edit_game_result_atomic must
    clamp at 0 instead of driving the balance negative."""
    game = await db.create_game_if_none_active(guild_id=881)
    users = [await _user(db, 500 + i) for i in range(10)]
    for u in users:
        await db.add_game_player_if_space(game.game_id, u.id, 10)
    blue, red = users[:5], users[5:]
    await db.set_captains(game.game_id, blue[0].id, red[0].id)
    for p in blue[1:]:
        await db.assign_team_if_available(game.game_id, p.id, "BLUE")
    for p in red[1:]:
        await db.assign_team_if_available(game.game_id, p.id, "RED")

    await db.transition_game_status(game.game_id, "WAITING", "DRAFT")
    await db.transition_game_status(game.game_id, "DRAFT", "PLAYING")
    await db.submit_screenshot_atomic(game.game_id, 3)

    win_map = {p.id: 70 for p in blue}
    loss_map = {p.id: 0 for p in red}
    ok = await db.finalize_game_result_atomic(
        game.game_id, "BLUE", 1,
        [p.id for p in blue], [p.id for p in red],
        25, -25, 0, win_map, loss_map,
    )
    assert ok
    winner = await db.get_user_by_id(blue[0].id)
    assert winner.vc_balance == 70

    # Player spends everything before the edit.
    await db.debit_vc(blue[0].id, 70, "SHOP_PURCHASE", "spent it all")
    spent = await db.get_user_by_id(blue[0].id)
    assert spent.vc_balance == 0

    new_win = {p.id: 55 for p in red}
    new_loss = {p.id: 10 for p in blue}
    edited = await db.edit_game_result_atomic(
        game.game_id,
        expected_current_winner="BLUE",
        new_winner="RED",
        confirmed_by=1,
        new_winner_ids=[p.id for p in red],
        new_loser_ids=[p.id for p in blue],
        elo_win=25,
        elo_loss=-25,
        min_elo=0,
        win_vc_map=new_win,
        loss_vc_map=new_loss,
    )
    assert edited is True

    after = await db.get_user_by_id(blue[0].id)
    # Balance must never go negative: the 70 VC revert is clamped at the
    # 0 that was actually available, then the new loss VC (10) is credited.
    assert after.vc_balance == 10
    assert after.vc_balance >= 0


async def test_admin_adjust_elo_concurrent_adds_commute(db):
    """BUG #12 regression: two concurrent /admin_elo add calls must not
    lose one of the two deltas the way admin_set_elo(old + amount) did."""
    import asyncio

    u = await _user(db, 900)
    await db.admin_set_elo(u.id, 100, 1, "seed")

    await asyncio.gather(
        db.admin_adjust_elo(u.id, 10, 1, "add:10"),
        db.admin_adjust_elo(u.id, 20, 1, "add:20"),
    )
    final = await db.get_user_by_id(u.id)
    assert final.elo == 130



    from services.casino import expected_rtp
    import config as cfg
    rtp = expected_rtp()
    assert abs(rtp - cfg.CASINO_RTP) < 0.02


async def test_single_elo_history_on_protection(db):
    game = await db.create_game_if_none_active(guild_id=89)
    users = [await _user(db, 300 + i) for i in range(10)]
    for u in users:
        await db.add_game_player_if_space(game.game_id, u.id, 10)
    blue, red = users[:5], users[5:]
    await db.set_captains(game.game_id, blue[0].id, red[0].id)
    for p in blue[1:]:
        await db.assign_team_if_available(game.game_id, p.id, "BLUE")
    for p in red[1:]:
        await db.assign_team_if_available(game.game_id, p.id, "RED")
    await db.credit_vc(red[0].id, 2000, "ADMIN_GRANT", "seed")
    await db.purchase_elo_protection(red[0].id, config.ELO_PROTECTION_PRICE)
    await db.transition_game_status(game.game_id, "WAITING", "DRAFT")
    await db.transition_game_status(game.game_id, "DRAFT", "PLAYING")
    await db.submit_screenshot_atomic(game.game_id, 4)
    await db.finalize_game_result_atomic(
        game.game_id, "BLUE", 1,
        [p.id for p in blue], [p.id for p in red],
        25, -25, 0,
        {p.id: 50 for p in blue}, {p.id: 10 for p in red},
    )
    async with db._conn.execute(
        "SELECT reason FROM elo_history WHERE player_id = ? AND game_id = ?",
        (red[0].id, game.game_id),
    ) as c:
        reasons = [r["reason"] for r in await c.fetchall()]
    # one game-related row, not two
    game_reasons = [r for r in reasons if r in ("game_result", "elo_protection")]
    assert len(game_reasons) == 1
    assert game_reasons[0] == "elo_protection"


async def test_casino_duplicate_interaction_rejected(db):
    """Fix #casino idempotency (migration 007): повторная передача того же
    interaction_id (например, Discord ретраит доставку клика) не должна
    списывать ставку дважды."""
    u = await _user(db, 400)
    await db.credit_vc(u.id, 1000, "ADMIN_GRANT", "seed")

    before, after = await db.play_casino_slots(
        u.id, 100, ("🍒", "🍒", "🍒"), 0, "LOSS", interaction_id="int-dup-1"
    )
    assert before == 1000
    assert after == 900

    with pytest.raises(ValueError, match="duplicate_interaction"):
        await db.play_casino_slots(
            u.id, 100, ("🍒", "🍒", "🍒"), 0, "LOSS", interaction_id="int-dup-1"
        )

    # Balance unaffected by the rejected duplicate.
    u2 = await db.get_user_by_id(u.id)
    assert u2.vc_balance == 900

    history = await db.get_casino_history(u.id, limit=10)
    assert len(history) == 1  # only the first spin was recorded


async def test_casino_concurrent_interactions_serialized(db):
    """Fix #casino concurrency (п.4/п.8 аудита): два параллельных спина
    одного игрока (два быстрых клика по разным ставкам) должны применяться
    последовательно, без потери одной из ставок и без ухода баланса в минус."""
    import asyncio

    u = await _user(db, 401)
    await db.credit_vc(u.id, 200, "ADMIN_GRANT", "seed")

    async def spin(tag: str):
        return await db.play_casino_slots(
            u.id, 100, ("🍋", "🍋", "🍒"), 0, "LOSS", interaction_id=f"concurrent-{tag}"
        )

    results = await asyncio.gather(spin("a"), spin("b"))
    # Both spins had enough balance in sequence (200 -> 100 -> 0), both succeed.
    balances_after = sorted(r[1] for r in results)
    assert balances_after == [0, 100]

    u2 = await db.get_user_by_id(u.id)
    assert u2.vc_balance == 0  # never negative, both bets applied exactly once

    history = await db.get_casino_history(u.id, limit=10)
    assert len(history) == 2


async def test_casino_ui_text_matches_backend_math():
    """Fix (аудит, п.8 — 'соответствие текста UI реальной математике'):
    раньше /casino обещал JACKPOT ×20 / тройка 2-8x / пара возврат 50%,
    а реальная таблица исходов давала другие числа. Формальный тест не
    может проверить текст embed'а напрямую (он строится в cogs/casino.py
    без доступа к discord Gateway здесь), но фиксирует сами числа, на
    которые теперь опирается текст, чтобы будущее изменение _OUTCOMES не
    разошлось с UI молча."""
    from services.casino import _OUTCOMES

    by_label = {label: mult for label, _, mult in _OUTCOMES}
    assert round(by_label["JACKPOT"], 2) == 10.67
    assert by_label["TRIPLE"] == 4.0
    assert by_label["PAIR"] == 1.0  # full refund, not 50%


async def test_shop_purchase_double_click_lock_allows_sequential_purchases(db):
    """cogs/shop.py теперь лочит покупку per-user (Fix, п.4 аудита —
    'shop double-click'). На уровне БД покупки остаются независимо
    атомарными — здесь проверяем, что два последовательных вызова с
    хватающим балансом на оба вполне ожидаемо проходят (лок не блокирует
    осознанные повторные покупки, только защищает от гонки одного клика)."""
    u = await _user(db, 402)
    await db.credit_vc(u.id, config.ELO_PROTECTION_PRICE * 2, "ADMIN_GRANT", "seed")

    bal1, prot1 = await db.purchase_elo_protection(u.id, config.ELO_PROTECTION_PRICE)
    assert prot1 == 1
    bal2, prot2 = await db.purchase_elo_protection(u.id, config.ELO_PROTECTION_PRICE)
    assert prot2 == 2
    assert bal2 == 0


async def test_edit_result_refund_uses_actual_clawed_amount(db):
    """REFUND ledger row must record the amount actually removed from the
    balance (capped at what the player still holds), not the original credit."""
    game = await db.create_game_if_none_active(guild_id=8801)
    users = [await _user(db, 500 + i) for i in range(10)]
    for u in users:
        await db.add_game_player_if_space(game.game_id, u.id, 10)
    blue, red = users[:5], users[5:]
    await db.set_captains(game.game_id, blue[0].id, red[0].id)
    for p in blue[1:]:
        await db.assign_team_if_available(game.game_id, p.id, "BLUE")
    for p in red[1:]:
        await db.assign_team_if_available(game.game_id, p.id, "RED")
    await db.transition_game_status(game.game_id, "WAITING", "DRAFT")
    await db.transition_game_status(game.game_id, "DRAFT", "PLAYING")
    await db.submit_screenshot_atomic(game.game_id, 99)

    win_map = {p.id: 70 for p in blue}
    loss_map = {p.id: 20 for p in red}
    ok = await db.finalize_game_result_atomic(
        game.game_id, "BLUE", 1,
        [p.id for p in blue], [p.id for p in red],
        25, -25, min_elo=0,
        win_vc_map=win_map, loss_vc_map=loss_map,
    )
    assert ok

    # Spend almost everything from one winner so clawback is partial.
    spender = blue[0]
    await db.debit_vc(spender.id, 60, "SHOP_PURCHASE", "spend most of reward")
    u_mid = await db.get_user_by_id(spender.id)
    assert u_mid.vc_balance == 10  # 70 - 60

    edited = await db.edit_game_result_atomic(
        game.game_id,
        expected_current_winner="BLUE",
        new_winner="RED",
        confirmed_by=1,
        new_winner_ids=[p.id for p in red],
        new_loser_ids=[p.id for p in blue],
        elo_win=25,
        elo_loss=-25,
        min_elo=0,
        win_vc_map={p.id: 50 for p in red},
        loss_vc_map={p.id: 10 for p in blue},
    )
    assert edited

    u_after = await db.get_user_by_id(spender.id)
    assert u_after.vc_balance >= 0

    txs = await db.get_coin_transactions(spender.id, limit=20)
    refunds = [t for t in txs if t.transaction_type == "REFUND"]
    assert refunds, "expected a REFUND row for partial clawback"
    # Clawed only what was left (10), not the original 70.
    assert refunds[0].amount == -10


async def test_has_later_completed_game_blocks_stale_edit(db):
    """BUG #15 guard: after a second COMPLETED game involving the same
    player, has_later_completed_game(first) must be True."""
    # Game 1
    g1 = await db.create_game_if_none_active(guild_id=8802)
    users = [await _user(db, 600 + i) for i in range(10)]
    for u in users:
        await db.add_game_player_if_space(g1.game_id, u.id, 10)
    blue, red = users[:5], users[5:]
    await db.set_captains(g1.game_id, blue[0].id, red[0].id)
    for p in blue[1:]:
        await db.assign_team_if_available(g1.game_id, p.id, "BLUE")
    for p in red[1:]:
        await db.assign_team_if_available(g1.game_id, p.id, "RED")
    await db.transition_game_status(g1.game_id, "WAITING", "DRAFT")
    await db.transition_game_status(g1.game_id, "DRAFT", "PLAYING")
    await db.submit_screenshot_atomic(g1.game_id, 1)
    assert await db.finalize_game_result_atomic(
        g1.game_id, "BLUE", 1,
        [p.id for p in blue], [p.id for p in red],
        25, -25,
    )

    assert not await db.has_later_completed_game(g1.game_id)

    # Game 2 with overlapping players
    g2 = await db.create_game_if_none_active(guild_id=8802)
    for u in users:
        await db.add_game_player_if_space(g2.game_id, u.id, 10)
    await db.set_captains(g2.game_id, blue[0].id, red[0].id)
    for p in blue[1:]:
        await db.assign_team_if_available(g2.game_id, p.id, "BLUE")
    for p in red[1:]:
        await db.assign_team_if_available(g2.game_id, p.id, "RED")
    await db.transition_game_status(g2.game_id, "WAITING", "DRAFT")
    await db.transition_game_status(g2.game_id, "DRAFT", "PLAYING")
    await db.submit_screenshot_atomic(g2.game_id, 2)
    assert await db.finalize_game_result_atomic(
        g2.game_id, "RED", 1,
        [p.id for p in red], [p.id for p in blue],
        25, -25,
    )

    assert await db.has_later_completed_game(g1.game_id)
    assert not await db.has_later_completed_game(g2.game_id)


async def test_set_captains_rejected_after_playing(db):
    """set_captains CAS: once the game leaves WAITING/DRAFT, captains freeze."""
    game = await db.create_game_if_none_active(guild_id=8803)
    users = [await _user(db, 700 + i) for i in range(2)]
    for u in users:
        await db.add_game_player_if_space(game.game_id, u.id, 10)
    assert await db.set_captains(game.game_id, users[0].id, users[1].id) is True
    await db.transition_game_status(game.game_id, "WAITING", "DRAFT")
    await db.transition_game_status(game.game_id, "DRAFT", "PLAYING")
    # Swap attempt must fail
    assert await db.set_captains(game.game_id, users[1].id, users[0].id) is False
    g = await db.get_game(game.game_id)
    assert g.blue_captain == users[0].id
    assert g.red_captain == users[1].id
