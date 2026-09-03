CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL UNIQUE,
    discord_username TEXT NOT NULL,
    mlbb_id TEXT,
    server_id TEXT,
    mlbb_nickname TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    elo INTEGER NOT NULL DEFAULT 100,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    games_played INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    game_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'WAITING',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    blue_captain INTEGER,
    red_captain INTEGER,
    winner TEXT,
    screenshot_message_id INTEGER,
    result_confirmed_by INTEGER,
    result_confirmed_at TEXT,
    blue_voice_channel_id INTEGER,
    red_voice_channel_id INTEGER,
    lobby_channel_id INTEGER,
    lobby_message_id INTEGER,
    draft_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS game_players (
    game_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    team TEXT,
    captain INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    elo_change INTEGER,
    PRIMARY KEY (game_id, player_id),
    FOREIGN KEY (game_id) REFERENCES games (game_id),
    FOREIGN KEY (player_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS elo_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL,
    game_id INTEGER,
    old_elo INTEGER NOT NULL,
    elo_change INTEGER NOT NULL,
    new_elo INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (player_id) REFERENCES users (id),
    FOREIGN KEY (game_id) REFERENCES games (game_id)
);
