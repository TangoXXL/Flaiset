CREATE TABLE IF NOT EXISTS queue_entries (
    guild_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    queued_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, player_id),
    FOREIGN KEY (player_id) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS idx_queue_entries_guild_order
ON queue_entries(guild_id, queued_at, player_id);
