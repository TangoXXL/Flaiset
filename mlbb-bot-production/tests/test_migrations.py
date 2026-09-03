"""
Тесты миграций (аудит, п.6): совместимость со старой схемой, идемпотентность,
отсутствие потери данных.
"""

import aiosqlite
import pytest

from database.database import Database, SCHEMA, _utcnow


async def test_fresh_db_applies_all_migrations(tmp_path):
    """Новая пустая БД — все миграции должны примениться без ошибок, и
    финальная схема должна содержать все ожидаемые столбцы/таблицы."""
    path = str(tmp_path / "fresh.db")
    db = Database(path)
    await db.connect()
    try:
        conn = db._conn
        async with conn.execute("PRAGMA table_info(games)") as cur:
            game_cols = {r["name"] for r in await cur.fetchall()}
        async with conn.execute("PRAGMA table_info(users)") as cur:
            user_cols = {r["name"] for r in await cur.fetchall()}
        async with conn.execute("PRAGMA table_info(casino_games)") as cur:
            casino_cols = {r["name"] for r in await cur.fetchall()}
        async with conn.execute("PRAGMA table_info(game_players)") as cur:
            gp_cols = {r["name"] for r in await cur.fetchall()}

        assert {"draft_pick_deadline", "confirm_notified_at"} <= game_cols
        assert {"vc_balance", "elo_protection"} <= user_cols
        assert "interaction_id" in casino_cols
        assert "protection_used" in gp_cols

        async with conn.execute("SELECT COUNT(*) AS c FROM schema_version") as cur:
            count = (await cur.fetchone())["c"]
            assert count == 8  # все восемь файлов миграций применены и зафиксированы
    finally:
        await db.close()


async def test_migrations_are_idempotent_on_reconnect(tmp_path):
    """Повторное подключение к уже мигрированной БД не должно ни падать,
    ни повторно применять уже применённые миграции (нет 'duplicate column')."""
    path = str(tmp_path / "reconnect.db")
    db1 = Database(path)
    await db1.connect()
    await db1.create_user_atomic(1, "Alice", "111", "1")
    await db1.close()

    # Второе подключение к тому же файлу — как рестарт процесса.
    db2 = Database(path)
    await db2.connect()  # не должно бросить aiosqlite.OperationalError
    try:
        user = await db2.get_user_by_discord_id(1)
        assert user is not None
        assert user.mlbb_id == "111"
    finally:
        await db2.close()


async def test_old_schema_without_schema_version_table_migrates_safely(tmp_path):
    """Симулирует БД самой первой версии бота — до появления schema_version
    и до полей lobby_channel_id/lobby_message_id/draft_message_id в games
    (bootstrap-ветка _apply_migrations: is_legacy and not has_versions).
    Данные пользователя должны пережить апгрейд без потерь."""
    path = str(tmp_path / "legacy.db")

    # Собираем БД вручную, как её создал бы самый первый релиз: только
    # базовые таблицы, БЕЗ recovery-полей и БЕЗ schema_version.
    legacy_schema = """
    CREATE TABLE users (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_id        INTEGER NOT NULL UNIQUE,
        discord_username  TEXT NOT NULL,
        mlbb_id           TEXT,
        server_id         TEXT,
        mlbb_nickname     TEXT,
        verified          INTEGER NOT NULL DEFAULT 0,
        elo               INTEGER NOT NULL DEFAULT 100,
        wins              INTEGER NOT NULL DEFAULT 0,
        losses            INTEGER NOT NULL DEFAULT 0,
        games_played      INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT NOT NULL
    );
    CREATE TABLE games (
        game_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id                INTEGER NOT NULL,
        status                  TEXT NOT NULL DEFAULT 'WAITING',
        created_at              TEXT NOT NULL,
        started_at              TEXT,
        finished_at             TEXT,
        blue_captain            INTEGER,
        red_captain             INTEGER,
        winner                  TEXT,
        screenshot_message_id   INTEGER,
        result_confirmed_by     INTEGER,
        result_confirmed_at     TEXT,
        blue_voice_channel_id   INTEGER,
        red_voice_channel_id    INTEGER
    );
    CREATE TABLE game_players (
        game_id     INTEGER NOT NULL,
        player_id   INTEGER NOT NULL,
        team        TEXT,
        captain     INTEGER NOT NULL DEFAULT 0,
        result      TEXT,
        elo_change  INTEGER,
        PRIMARY KEY (game_id, player_id)
    );
    CREATE TABLE elo_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id   INTEGER NOT NULL,
        game_id     INTEGER,
        old_elo     INTEGER NOT NULL,
        elo_change  INTEGER NOT NULL,
        new_elo     INTEGER NOT NULL,
        reason      TEXT,
        created_at  TEXT NOT NULL
    );
    """
    raw = await aiosqlite.connect(path)
    try:
        await raw.executescript(legacy_schema)
        await raw.execute(
            "INSERT INTO users (discord_id, discord_username, mlbb_id, server_id, elo, wins, losses, "
            "games_played, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (42, "OldPlayer", "999999", "1", 150, 3, 1, 4, _utcnow()),
        )
        await raw.commit()
    finally:
        await raw.close()

    db = Database(path)
    await db.connect()  # запускает bootstrap-ветку + все 7 миграций поверх
    try:
        user = await db.get_user_by_discord_id(42)
        assert user is not None
        assert user.mlbb_id == "999999"
        assert user.elo == 150
        assert user.wins == 3
        # Новые поля должны появиться со значениями по умолчанию, старые данные не тронуты.
        assert user.vc_balance == 0
        assert user.elo_protection == 0

        conn = db._conn
        async with conn.execute("PRAGMA table_info(games)") as cur:
            game_cols = {r["name"] for r in await cur.fetchall()}
        assert {"lobby_channel_id", "lobby_message_id", "draft_message_id",
                "draft_pick_deadline", "confirm_notified_at"} <= game_cols

        async with conn.execute("SELECT version FROM schema_version ORDER BY version") as cur:
            versions = [r["version"] for r in await cur.fetchall()]
        assert versions == [1, 2, 3, 4, 5, 6, 7, 8]
    finally:
        await db.close()


async def test_partially_applied_migrations_resume_from_correct_version(tmp_path):
    """БД, на которой уже применены миграции 1-5 (например, бот был
    обновлён не до последней версии), при подключении новой версией кода
    должна доехать ровно до 006/007, не пытаясь повторно накатить 1-5."""
    path = str(tmp_path / "partial.db")

    raw = await aiosqlite.connect(path)
    try:
        await raw.executescript(SCHEMA)
        await raw.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        now = _utcnow()
        for v in (1, 2, 3, 4, 5):
            await raw.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, ?)", (v, now))
        # Миграции 002-005 в реальности добавляют индексы/столбцы — здесь
        # достаточно смоделировать столбцы 004/005, раз SCHEMA их не содержит.
        await raw.execute("ALTER TABLE games ADD COLUMN draft_pick_deadline TEXT")
        await raw.execute("ALTER TABLE games ADD COLUMN confirm_notified_at TEXT")
        await raw.execute(
            "CREATE TABLE queue_entries (guild_id INTEGER NOT NULL, player_id INTEGER NOT NULL, "
            "queued_at TEXT NOT NULL, PRIMARY KEY (guild_id, player_id))"
        )
        await raw.execute(
            "INSERT INTO users (discord_id, discord_username, mlbb_id, server_id, elo, created_at) "
            "VALUES (7, 'Partial', '12345', '1', 100, ?)",
            (now,),
        )
        await raw.commit()
    finally:
        await raw.close()

    db = Database(path)
    await db.connect()
    try:
        conn = db._conn
        async with conn.execute("SELECT version FROM schema_version ORDER BY version") as cur:
            versions = [r["version"] for r in await cur.fetchall()]
        assert versions == [1, 2, 3, 4, 5, 6, 7, 8]

        user = await db.get_user_by_discord_id(7)
        assert user is not None
        assert user.vc_balance == 0  # добавлено миграцией 006, применённой сейчас

        async with conn.execute("PRAGMA table_info(casino_games)") as cur:
            casino_cols = {r["name"] for r in await cur.fetchall()}
        assert "interaction_id" in casino_cols  # применена миграция 007
    finally:
        await db.close()


async def test_reapplying_migration_file_manually_is_a_noop_error_free(tmp_path):
    """Если бы _apply_migrations попытался применить уже отмеченную версию
    повторно, executescript упал бы на 'duplicate column'/'already exists'.
    Тест фиксирует, что схема-версии реально предотвращают это: второй
    коннект к той же базе не бросает исключений."""
    path = str(tmp_path / "noop.db")
    db1 = Database(path)
    await db1.connect()
    await db1.close()

    db2 = Database(path)
    try:
        await db2.connect()
    except Exception as exc:  # pragma: no cover - должно не сработать
        pytest.fail(f"Повторное подключение не должно падать: {exc!r}")
    else:
        await db2.close()
