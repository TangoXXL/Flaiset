"""Слоты с целевым RTP ≈ config.CASINO_RTP (таблица исходов + secrets RNG)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

import config


@dataclass(frozen=True)
class SlotSpin:
    symbols: tuple[str, str, str]
    bet: int
    payout: int
    result: str  # LOSS | PAIR | TRIPLE | JACKPOT


# EV ≈ 0.95:
# LOSS 0x @ 70%, PAIR 1x @ 15%, TRIPLE 4x @ 12%, JACKPOT ~10.67x @ 3%
_OUTCOMES: tuple[tuple[str, int, float], ...] = (
    ("LOSS", 700, 0.0),
    ("PAIR", 150, 1.0),
    ("TRIPLE", 120, 4.0),
    ("JACKPOT", 30, 10.6667),
)


def expected_rtp() -> float:
    total_w = sum(w for _, w, _ in _OUTCOMES)
    return sum((w / total_w) * m for _, w, m in _OUTCOMES)


def spin_slots(bet: int) -> SlotSpin:
    if bet not in config.CASINO_ALLOWED_BETS:
        raise ValueError("invalid_bet")
    if bet < config.CASINO_MIN_BET or bet > config.CASINO_MAX_BET:
        raise ValueError("invalid_bet")

    total_w = sum(w for _, w, _ in _OUTCOMES)
    r = secrets.randbelow(total_w)
    acc = 0
    label, mult = "LOSS", 0.0
    for lab, w, m in _OUTCOMES:
        acc += w
        if r < acc:
            label, mult = lab, m
            break

    payout = int(round(bet * mult))
    return SlotSpin(_symbols_for(label), bet, payout, label)


def _symbols_for(label: str) -> tuple[str, str, str]:
    pool = list(config.CASINO_SLOT_SYMBOLS)
    if label == "JACKPOT":
        return ("💎", "💎", "💎")
    if label == "TRIPLE":
        choices = [s for s in pool if s != "💎"] or pool
        s = choices[secrets.randbelow(len(choices))]
        return (s, s, s)
    if label == "PAIR":
        s = pool[secrets.randbelow(len(pool))]
        other = pool[secrets.randbelow(len(pool))]
        while other == s:
            other = pool[secrets.randbelow(len(pool))]
        pos = secrets.randbelow(3)
        if pos == 0:
            return (s, s, other)
        if pos == 1:
            return (s, other, s)
        return (other, s, s)
    # BUG #8 fix: a LOSS spin must show three genuinely DIFFERENT symbols.
    # The old code only re-rolled when all three matched (a triple), so a
    # LOSS could still land on e.g. 🍒🍒🍋 — visually a pair — telling the
    # player they won when the ledger says they didn't. Guarantee pairwise
    # distinctness for LOSS reels instead.
    if len(pool) < 3:
        # Degenerate config safeguard; not expected in practice.
        return (pool[0], pool[0], pool[0])
    a = pool[secrets.randbelow(len(pool))]
    b = a
    while b == a:
        b = pool[secrets.randbelow(len(pool))]
    c = a
    while c == a or c == b:
        c = pool[secrets.randbelow(len(pool))]
    return (a, b, c)


CONFIGURED_RTP = expected_rtp()
