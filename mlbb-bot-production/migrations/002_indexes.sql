CREATE UNIQUE INDEX IF NOT EXISTS idx_users_mlbb_unique
ON users(mlbb_id, server_id)
WHERE mlbb_id IS NOT NULL AND server_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_games_one_active_per_guild
ON games(guild_id)
WHERE status NOT IN ('COMPLETED', 'CANCELLED');

CREATE INDEX IF NOT EXISTS idx_game_players_player_game
ON game_players(player_id, game_id);

CREATE INDEX IF NOT EXISTS idx_games_completed_finished
ON games(status, finished_at DESC);
