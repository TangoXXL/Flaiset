-- Track protection consumption per match for correct /edit_result restore
ALTER TABLE game_players ADD COLUMN protection_used INTEGER NOT NULL DEFAULT 0;

-- Casino interaction idempotency (Discord interaction id)
ALTER TABLE casino_games ADD COLUMN interaction_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_interaction_unique
ON casino_games(interaction_id)
WHERE interaction_id IS NOT NULL;
