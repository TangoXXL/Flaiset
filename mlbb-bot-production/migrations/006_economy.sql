-- Valhalla Coin + Elo Protection + casino history

ALTER TABLE users ADD COLUMN vc_balance INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN elo_protection INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS coin_transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    amount          INTEGER NOT NULL,
    balance_after   INTEGER NOT NULL,
    transaction_type TEXT NOT NULL,
    description     TEXT,
    game_id         INTEGER,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (game_id) REFERENCES games (game_id)
);

CREATE INDEX IF NOT EXISTS idx_coin_tx_user_created
ON coin_transactions(user_id, created_at DESC);

-- Idempotency: one MATCH_* reward per (user, game)
CREATE UNIQUE INDEX IF NOT EXISTS idx_coin_tx_match_once
ON coin_transactions(user_id, game_id, transaction_type)
WHERE game_id IS NOT NULL AND transaction_type IN ('MATCH_WIN', 'MATCH_LOSS');

CREATE TABLE IF NOT EXISTS casino_games (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    game_type       TEXT NOT NULL,
    bet             INTEGER NOT NULL,
    result          TEXT NOT NULL,
    payout          INTEGER NOT NULL,
    balance_before  INTEGER NOT NULL,
    balance_after   INTEGER NOT NULL,
    details         TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS idx_casino_user_created
ON casino_games(user_id, created_at DESC);
