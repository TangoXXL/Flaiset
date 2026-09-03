"""
Слой доступа к SQLite — единая точка работы с базой для всего бота.

Используем aiosqlite, а не стандартный sqlite3, потому что бот целиком
асинхронный (discord.py работает на asyncio): синхронный sqlite3.connect()
внутри async-кода блокировал бы event loop и "подвешивал" бота при
каждом обращении к базе.

Схема создаёт СРАЗУ все таблицы из ТЗ (users, games, game_players,
elo_history). Новые столбцы, добавленные в процессе разработки (после
Этапа 5), докатываются на уже существующие БД через _migrate().

Небольшое осознанное отступление от ТЗ (п.40): в списке статусов там
есть WAITING_RESULT между PLAYING и PENDING_CONFIRMATION. Мы его не
используем — переход происходит сразу PLAYING -> PENDING_CONFIRMATION
в момент получения скриншота, потому что "ожидание результата" и
"идёт игра" неразличимы для бота до этого момента (нет отдельного
действия, которое стоило бы помечать промежуточным статусом).
"""

import asyncio
import functools
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from database.models import Game, MatchRecord, QueueEntry, User

log = logging.getLogger("mlbb-bot.database")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
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

CREATE TABLE IF NOT EXISTS games (
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
    red_voice_channel_id    INTEGER,
    lobby_channel_id        INTEGER,
    lobby_message_id        INTEGER,
    draft_message_id        INTEGER
);

CREATE TABLE IF NOT EXISTS game_players (
    game_id     INTEGER NOT NULL,
    player_id   INTEGER NOT NULL,
    team        TEXT,
    captain     INTEGER NOT NULL DEFAULT 0,
    result      TEXT,
    elo_change  INTEGER,
    PRIMARY KEY (game_id, player_id),
    FOREIGN KEY (game_id) REFERENCES games (game_id),
    FOREIGN KEY (player_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS elo_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   INTEGER NOT NULL,
    game_id     INTEGER,
    old_elo     INTEGER NOT NULL,
    elo_change  INTEGER NOT NULL,
    new_elo     INTEGER NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (player_id) REFERENCES users (id),
    FOREIGN KEY (game_id) REFERENCES games (game_id)
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialized_write(method):
    """Serialize commits on the single shared aiosqlite connection.

    aiosqlite serializes individual SQL calls, not a sequence of calls that
    forms a transaction.  Without this lock, an unrelated writer can commit
    or roll back the transaction between two awaits in a result finalization.

    BUG #10 fix: several write methods (set_captains, set_lobby_message,
    transition_game_status, assign_team, ...) run multiple statements and a
    single commit() but have NO try/except of their own. If any statement
    raises mid-sequence (e.g. a transient "database is locked", a driver
    error), the connection is left with an open, partially-applied
    transaction and no ROLLBACK is ever issued — the next unrelated write on
    this same shared connection could then either inherit and accidentally
    commit that dangling partial state, or itself fail in confusing ways.
    Every @_serialized_write method now gets an automatic rollback-on-
    exception wrapper, so a failure anywhere always leaves the connection in
    a clean, transaction-free state before the lock is released.
    """
    @functools.wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self._write_lock:
            try:
                return await method(self, *args, **kwargs)
            except Exception:
                if self._conn is not None:
                    try:
                        await self._conn.rollback()
                    except Exception:
                        log.exception(
                            "Не удалось выполнить ROLLBACK после ошибки в %s — "
                            "соединение может быть в неконсистентном состоянии",
                            method.__name__,
                        )
                raise
    return wrapped


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        # Внешние ключи в SQLite по умолчанию выключены — включаем явно.
        await self._conn.execute("PRAGMA foreign_keys = ON;")
        # WAL позволяет читателям не блокироваться на писателе (и наоборот) —
        # важно, если базу параллельно откроет ещё один процесс (например,
        # sqlite3 CLI для отладки) или инструмент бэкапа. Сам бот использует
        # ОДНО соединение, и aiosqlite сериализует все операции на нём через
        # внутренний поток-воркер, поэтому WAL не устраняет гонки внутри
        # процесса (для этого используются атомарные UPDATE...WHERE и
        # транзакции ниже) — он лишь защищает от "database is locked" при
        # внешнем параллельном доступе к файлу базы.
        await self._conn.execute("PRAGMA journal_mode = WAL;")
        # busy_timeout: если файл базы всё же занят другим соединением
        # (внешний процесс, тот же бэкап), даём SQLite подождать вместо
        # немедленного OperationalError("database is locked").
        await self._conn.execute("PRAGMA busy_timeout = 5000;")
        await self._apply_migrations()  
        log.info("База данных подключена: %s", self.path)

    async def _apply_migrations(self) -> None:
        """Применяет SQL-миграции строго по версии, без удаления данных."""
        assert self._conn is not None
        await self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )

        async with self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'games'"
        ) as cursor:
            is_legacy = await cursor.fetchone() is not None

        async with self._conn.execute("SELECT COUNT(*) AS count FROM schema_version") as cursor:
            has_versions = (await cursor.fetchone())["count"] > 0

        # Базы старой версии не имели schema_version. Помечаем initial как
        # пройденную, предварительно добавив отсутствующие recovery-поля по
        # introspection, а не через исключение duplicate column.
        if is_legacy and not has_versions:
            async with self._conn.execute("PRAGMA table_info(games)") as cursor:
                columns = {row["name"] for row in await cursor.fetchall()}
            for name in ("lobby_channel_id", "lobby_message_id", "draft_message_id"):
                if name not in columns:
                    await self._conn.execute(f"ALTER TABLE games ADD COLUMN {name} INTEGER")
            await self._conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (1, ?)", (_utcnow(),)
            )
            await self._conn.commit()

        migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
        for path in sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql")):
            version = int(path.name[:3])
            async with self._conn.execute(
                "SELECT 1 FROM schema_version WHERE version = ?", (version,)
            ) as cursor:
                applied = await cursor.fetchone() is not None
            if applied:
                continue
            try:
                await self._conn.executescript(path.read_text(encoding="utf-8"))
                await self._conn.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                    (version, _utcnow()),
                )
                await self._conn.commit()
                log.info("Применена миграция %s", path.name)
            except Exception:
                await self._conn.rollback()
                log.exception("Не удалось применить миграцию %s", path.name)
                raise

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            log.info("Соединение с базой данных закрыто")

    async def backup_to(self, dest_path: str) -> None:
        """Безопасный бэкап через встроенный SQLite backup API (а не простое
        копирование файла). Работает корректно даже если в этот момент
        идёт запись — в отличие от shutil.copy файла "на живую"."""
        assert self._conn is not None
        dest_conn = await aiosqlite.connect(dest_path)
        try:
            await self._conn.backup(dest_conn)
        finally:
            await dest_conn.close()

    # --- Пользователи --------------------------------------------------

    async def get_user_by_discord_id(self, discord_id: int) -> Optional[User]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM users WHERE discord_id = ?", (discord_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return User.from_row(row) if row else None

    @_serialized_write
    async def create_user(
        self,
        discord_id: int,
        discord_username: str,
        mlbb_id: str,
        server_id: str,
        starting_elo: int = 100,
    ) -> User:
        """Создаёт нового игрока. Вызывающий код должен сам проверить,
        что игрок ещё не зарегистрирован (get_user_by_discord_id).

        ВНИМАНИЕ: не защищена от гонки — если между проверкой в вызывающем
        коде и этим INSERT кто-то другой успеет зарегистрировать того же
        discord_id или тот же MLBB ID, здесь вылетит необработанный
        aiosqlite.IntegrityError. Используй create_user_atomic там, где
        важно не уронить обработчик интеракции с generic ошибкой
        (см. Fix round 3, п.5 ревью)."""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO users
                (discord_id, discord_username, mlbb_id, server_id, elo, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (discord_id, discord_username, mlbb_id, server_id, starting_elo, _utcnow()),
        )
        await self._conn.commit()
        user = await self.get_user_by_discord_id(discord_id)
        assert user is not None
        log.info("Зарегистрирован новый игрок: discord_id=%s mlbb_id=%s", discord_id, mlbb_id)
        return user

    @_serialized_write
    async def create_user_atomic(
        self,
        discord_id: int,
        discord_username: str,
        mlbb_id: str,
        server_id: str,
        starting_elo: int = 100,
    ) -> Optional[User]:
        """Fix (round 3, критический — требование п.5 ревью): атомарная
        версия create_user, которая ловит IntegrityError вместо того,
        чтобы позволить ему всплыть наружу.

        Race condition, который это устраняет: два почти одновременных
        сабмита формы /register от одного и того же пользователя (двойной
        клик, ретрай интеракции со стороны Discord и т.п.) — оба проходят
        проверку "existing is None" (см. cogs/registration.py) ДО того,
        как первый успевает закоммитить свой INSERT; второй INSERT тогда
        нарушает UNIQUE(discord_id) и раньше падал необработанным
        исключением, которое Discord показывал пользователю как
        малопонятную "Interaction failed" ошибку. То же самое возможно и
        для UNIQUE(mlbb_id, server_id), если два РАЗНЫХ пользователя
        почти одновременно пытаются занять один и тот же MLBB-аккаунт.

        Возвращает созданного User при успехе, или None при конфликте —
        вызывающий код (cogs/registration.py) сам решает, какое понятное
        сообщение показать, заново проверив, что именно произошло."""
        assert self._conn is not None
        try:
            await self._conn.execute(
                """
                INSERT INTO users
                    (discord_id, discord_username, mlbb_id, server_id, elo, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (discord_id, discord_username, mlbb_id, server_id, starting_elo, _utcnow()),
            )
            await self._conn.commit()
        except aiosqlite.IntegrityError:
            await self._conn.rollback()
            log.warning(
                "create_user_atomic: гонка при регистрации discord_id=%s mlbb_id=%s server=%s — отклонено",
                discord_id, mlbb_id, server_id,
            )
            return None
        user = await self.get_user_by_discord_id(discord_id)
        assert user is not None
        log.info("Зарегистрирован новый игрок: discord_id=%s mlbb_id=%s", discord_id, mlbb_id)
        return user

    @_serialized_write
    async def update_mlbb_info(self, discord_id: int, mlbb_id: str, server_id: str) -> None:
        """Позволяет игроку перерегистрировать/обновить свой MLBB ID.

        ВНИМАНИЕ: не защищена от гонки с уникальным индексом
        users(mlbb_id, server_id) — если между проверкой владельца
        (get_user_by_mlbb) и этим UPDATE кто-то другой успеет занять тот
        же MLBB ID, здесь вылетит необработанный aiosqlite.IntegrityError.
        Используй update_mlbb_info_atomic там, где важно не уронить
        обработчик интеракции (см. Fix round 3, п.5 ревью)."""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE users SET mlbb_id = ?, server_id = ? WHERE discord_id = ?",
            (mlbb_id, server_id, discord_id),
        )
        await self._conn.commit()

    @_serialized_write
    async def update_mlbb_info_atomic(self, discord_id: int, mlbb_id: str, server_id: str) -> bool:
        """Fix (round 3, критический — требование п.5 ревью): та же
        операция, что и update_mlbb_info, но ловит IntegrityError вместо
        того, чтобы позволить ему всплыть наружу и уронить обработчик
        интеракции с generic "Interaction failed" ошибкой в Discord.

        Гонка возможна так: игрок A проверяет (get_user_by_mlbb), что
        MLBB ID свободен, и в этот же момент игрок B успевает занять
        именно этот MLBB ID (двойной сабмит формы, повторный клик и т.п.)
        между проверкой A и его UPDATE. Уникальный частичный индекс
        idx_users_mlbb_unique в БД — последняя линия защиты; здесь мы
        только аккуратно ловим её срабатывание.

        Возвращает True при успехе, False — если MLBB ID оказался занят
        буквально в последний момент (кто-то опередил)."""
        assert self._conn is not None
        try:
            await self._conn.execute(
                "UPDATE users SET mlbb_id = ?, server_id = ? WHERE discord_id = ?",
                (mlbb_id, server_id, discord_id),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            await self._conn.rollback()
            log.warning(
                "update_mlbb_info_atomic: гонка за MLBB ID=%s server=%s (discord_id=%s) — отклонено",
                mlbb_id, server_id, discord_id,
            )
            return False

    async def get_user_by_mlbb(self, mlbb_id: str, server_id: str) -> Optional[User]:
        """Fix #8: ищет пользователя по паре MLBB ID + Server ID — используется
        перед регистрацией/обновлением, чтобы не дать двум Discord-аккаунтам
        занять один и тот же MLBB-аккаунт."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM users WHERE mlbb_id = ? AND server_id = ?", (mlbb_id, server_id)
        ) as cursor:
            row = await cursor.fetchone()
        return User.from_row(row) if row else None

    @_serialized_write
    async def sync_username(self, discord_id: int, discord_username: str) -> None:
        """Discord username может меняться — обновляем его при каждом взаимодействии,
        не полагаясь на него как на идентификатор (см. ТЗ, п.5)."""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE users SET discord_username = ? WHERE discord_id = ?",
            (discord_username, discord_id),
        )
        await self._conn.commit()

    async def get_leaderboard(self, limit: int = 10) -> list[User]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM users ORDER BY elo DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [User.from_row(row) for row in rows]

    # --- Игры (Этап 8+) -------------------------------------------------
    # ВАЖНО (ТЗ, п.45): на этом этапе поддерживается только одна активная
    # игра на сервер одновременно. "Активная" = статус не COMPLETED и не CANCELLED.

    ACTIVE_STATUSES = ("WAITING", "DRAFT", "PLAYING", "WAITING_RESULT", "PENDING_CONFIRMATION")

    async def get_active_game(self, guild_id: int) -> Optional[Game]:
        assert self._conn is not None
        placeholders = ",".join("?" for _ in self.ACTIVE_STATUSES)
        async with self._conn.execute(
            f"SELECT * FROM games WHERE guild_id = ? AND status IN ({placeholders}) "
            "ORDER BY game_id DESC LIMIT 1",
            (guild_id, *self.ACTIVE_STATUSES),
        ) as cursor:
            row = await cursor.fetchone()
        return Game.from_row(row) if row else None

    @_serialized_write
    async def create_game(self, guild_id: int) -> Game:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "INSERT INTO games (guild_id, status, created_at) VALUES (?, 'WAITING', ?)",
            (guild_id, _utcnow()),
        )
        await self._conn.commit()
        game_id = cursor.lastrowid
        return await self.get_game(game_id)

    @_serialized_write
    async def create_game_if_none_active(self, guild_id: int) -> Optional[Game]:
        """Fix (round 2, критический): атомарно создаёт игру, только если
        на этом guild_id сейчас нет другой активной игры. Полагается на
        уникальный частичный индекс idx_games_one_active_per_guild — если
        вставка нарушает его (кто-то создал активную игру буквально
        мгновением раньше), ловим IntegrityError и возвращаем None вместо
        падения. Это устраняет гонку двух администраторов, почти
        одновременно вызвавших /game."""
        assert self._conn is not None
        try:
            cursor = await self._conn.execute(
                "INSERT INTO games (guild_id, status, created_at) VALUES (?, 'WAITING', ?)",
                (guild_id, _utcnow()),
            )
            await self._conn.commit()
        except aiosqlite.IntegrityError:
            await self._conn.rollback()
            return None
        game_id = cursor.lastrowid
        return await self.get_game(game_id)

    async def get_game(self, game_id: int) -> Optional[Game]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM games WHERE game_id = ?", (game_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return Game.from_row(row) if row else None

    @_serialized_write
    async def update_game_status(self, game_id: int, status: str) -> None:
        assert self._conn is not None
        column = None
        if status == "PLAYING":
            column = "started_at"
        elif status in ("COMPLETED", "CANCELLED"):
            column = "finished_at"

        if column:
            await self._conn.execute(
                f"UPDATE games SET status = ?, {column} = ? WHERE game_id = ?",
                (status, _utcnow(), game_id),
            )
        else:
            await self._conn.execute(
                "UPDATE games SET status = ? WHERE game_id = ?", (status, game_id)
            )
        await self._conn.commit()

    @_serialized_write
    async def transition_game_status(self, game_id: int, from_status: str, to_status: str) -> bool:
        """Fix #3/#4 (сопутствующая защита): атомарный переход статуса,
        применяется, только если текущий статус в БД РАВЕН from_status.
        Возвращает False, если переход уже был сделан другим конкурентным
        вызовом (например, набор закрылся дважды из-за одновременных
        нажатий "Вступить") — тогда вызывающий код должен просто ничего
        не делать повторно."""
        assert self._conn is not None
        column = None
        if to_status == "PLAYING":
            column = "started_at"
        elif to_status in ("COMPLETED", "CANCELLED"):
            column = "finished_at"

        if column:
            cursor = await self._conn.execute(
                f"UPDATE games SET status = ?, {column} = ? WHERE game_id = ? AND status = ?",
                (to_status, _utcnow(), game_id, from_status),
            )
        else:
            cursor = await self._conn.execute(
                "UPDATE games SET status = ? WHERE game_id = ? AND status = ?",
                (to_status, game_id, from_status),
            )
        await self._conn.commit()
        return cursor.rowcount > 0

    @_serialized_write
    async def cancel_active_game_atomic(self, game_id: int) -> bool:
        """Fix (round 3, критический): атомарно переводит игру в CANCELLED,
        только если её статус ещё НЕ терминальный (не COMPLETED и не
        CANCELLED). Защищает /cancel_game от гонки с обычным завершением
        игры (подтверждение результата) — если результат уже был
        подтверждён (и ELO уже начислен) буквально мгновением раньше,
        эта функция вернёт False, и /cancel_game не перезапишет
        COMPLETED на CANCELLED поверх уже применённого результата."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE games SET status = 'CANCELLED', finished_at = ? "
            "WHERE game_id = ? AND status NOT IN ('COMPLETED', 'CANCELLED')",
            (_utcnow(), game_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    # --- Восстановление после перезапуска (Этап 39) ----------------------

    @_serialized_write
    async def set_lobby_message(self, game_id: int, channel_id: int, message_id: int) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE games SET lobby_channel_id = ?, lobby_message_id = ? WHERE game_id = ?",
            (channel_id, message_id, game_id),
        )
        await self._conn.commit()

    @_serialized_write
    async def set_draft_message(self, game_id: int, message_id: int) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE games SET draft_message_id = ? WHERE game_id = ?", (message_id, game_id)
        )
        await self._conn.commit()

    @_serialized_write
    async def set_draft_pick_deadline(self, game_id: int, deadline_iso: str | None) -> None:
        """Сохраняет дедлайн текущего хода драфта (ISO UTC), чтобы после
        рестарта бота таймер продолжался с оставшимся временем, а не с нуля."""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE games SET draft_pick_deadline = ? WHERE game_id = ?",
            (deadline_iso, game_id),
        )
        await self._conn.commit()

    @_serialized_write
    async def set_confirm_notified_at(self, game_id: int, notified_at_iso: str) -> None:
        """Метка последней рассылки DM админам по PENDING_CONFIRMATION.
        Нужна, чтобы recover после crash-loop не спамил одними и теми же
        запросами каждые несколько секунд."""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE games SET confirm_notified_at = ? WHERE game_id = ?",
            (notified_at_iso, game_id),
        )
        await self._conn.commit()

    async def get_games_by_statuses(self, statuses: tuple[str, ...]) -> list[Game]:
        """Используется при старте бота, чтобы найти игры, которые были
        активны в момент остановки/перезапуска, и восстановить их состояние."""
        assert self._conn is not None
        placeholders = ",".join("?" for _ in statuses)
        async with self._conn.execute(
            f"SELECT * FROM games WHERE status IN ({placeholders})", statuses
        ) as cursor:
            rows = await cursor.fetchall()
        return [Game.from_row(row) for row in rows]

    # --- Участники набора / игры ----------------------------------------

    @_serialized_write
    async def add_game_player(self, game_id: int, player_internal_id: int) -> None:
        """player_internal_id — это users.id (НЕ discord_id).

        ВНИМАНИЕ: не защищена от превышения лимита игроков при гонке —
        используй add_game_player_if_space там, где важно не допустить
        11/10 (см. Fix #3). Оставлена для мест, где переполнение
        невозможно по построению."""
        assert self._conn is not None
        await self._conn.execute(
            "INSERT OR IGNORE INTO game_players (game_id, player_id, captain) VALUES (?, ?, 0)",
            (game_id, player_internal_id),
        )
        await self._conn.commit()

    @_serialized_write
    async def add_game_player_if_space(self, game_id: int, player_internal_id: int, max_players: int) -> bool:
        """Fix #3: атомарно добавляет игрока в набор, только если сейчас
        меньше max_players участников и этот игрок ещё не в наборе.

        Проверка количества и вставка выполняются ОДНИМ SQL-выражением
        (INSERT ... SELECT ... WHERE), поэтому между "проверить" и
        "добавить" нет точки, в которой event loop мог бы переключиться
        на другую корутину — гонка из двух одновременных нажатий
        "Вступить" физически невозможна: SQLite обработает второй вызов
        уже видя результат первого.

        Возвращает True, если игрок был добавлен; False — если мест не
        осталось или игрок уже в наборе."""
        assert self._conn is not None
        # BUG #3 / BUG #13 fix: also require games.status = 'WAITING' AND
        # that the player isn't currently sitting in this guild's queue —
        # both checked in the same atomic INSERT...SELECT as the count/
        # membership checks, so a join can't race either a WAITING→DRAFT
        # transition or a concurrent /queue join into an inconsistent
        # double-membership state.
        cursor = await self._conn.execute(
            """
            INSERT INTO game_players (game_id, player_id, captain)
            SELECT ?, ?, 0
            WHERE (SELECT COUNT(*) FROM game_players WHERE game_id = ?) < ?
              AND NOT EXISTS (SELECT 1 FROM game_players WHERE game_id = ? AND player_id = ?)
              AND (SELECT status FROM games WHERE game_id = ?) = 'WAITING'
              AND NOT EXISTS (
                  SELECT 1 FROM queue_entries
                  WHERE player_id = ?
                    AND guild_id = (SELECT guild_id FROM games WHERE game_id = ?)
              )
            """,
            (
                game_id, player_internal_id, game_id, max_players, game_id, player_internal_id,
                game_id, player_internal_id, game_id,
            ),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    @_serialized_write
    async def remove_game_player(self, game_id: int, player_internal_id: int) -> bool:
        """BUG #3 fix: only allow leaving while the game is still WAITING.
        Once the game has moved to DRAFT/PLAYING/etc, a stray leave (e.g. a
        request that raced the WAITING→DRAFT transition) must not silently
        remove a drafted/captain player. Returns True if a row was actually
        deleted, so callers can detect and report the "too late to leave"
        case instead of assuming success."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            DELETE FROM game_players
            WHERE game_id = ? AND player_id = ?
              AND (SELECT status FROM games WHERE game_id = ?) = 'WAITING'
            """,
            (game_id, player_internal_id, game_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def is_player_in_game(self, game_id: int, player_internal_id: int) -> bool:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT 1 FROM game_players WHERE game_id = ? AND player_id = ?",
            (game_id, player_internal_id),
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None

    async def get_game_players(self, game_id: int) -> list[User]:
        """Возвращает список участников набора как объекты User,
        в порядке присоединения (по game_players.rowid)."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT users.* FROM game_players
            JOIN users ON users.id = game_players.player_id
            WHERE game_players.game_id = ?
            ORDER BY game_players.rowid ASC
            """,
            (game_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [User.from_row(row) for row in rows]

    async def count_game_players(self, game_id: int) -> int:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM game_players WHERE game_id = ?", (game_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["cnt"]

    # --- Капитаны и драфт (Этап 11-12) ----------------------------------

    @_serialized_write
    async def set_captains(self, game_id: int, blue_captain_id: int, red_captain_id: int) -> bool:
        """blue_captain_id / red_captain_id — это users.id (внутренний), не discord_id.

        CAS: applies only while the game is still WAITING or DRAFT. Returns
        True if captains were set, False if the game was already past that
        window (cancel / finalize raced us).
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE games SET blue_captain = ?, red_captain = ? "
            "WHERE game_id = ? AND status IN ('WAITING', 'DRAFT')",
            (blue_captain_id, red_captain_id, game_id),
        )
        if cursor.rowcount == 0:
            await self._conn.commit()
            return False
        await self._conn.execute(
            "UPDATE game_players SET team = 'BLUE', captain = 1 WHERE game_id = ? AND player_id = ?",
            (game_id, blue_captain_id),
        )
        await self._conn.execute(
            "UPDATE game_players SET team = 'RED', captain = 1 WHERE game_id = ? AND player_id = ?",
            (game_id, red_captain_id),
        )
        await self._conn.commit()
        return True

    @_serialized_write
    async def assign_team(self, game_id: int, player_id: int, team: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE game_players SET team = ? WHERE game_id = ? AND player_id = ?",
            (team, game_id, player_id),
        )
        await self._conn.commit()

    @_serialized_write
    async def assign_team_if_available(self, game_id: int, player_id: int, team: str) -> bool:
        """Fix #4: атомарно назначает игрока в команду, только если он ещё
        никому не назначен (team IS NULL). Защищает от гонки двух быстрых
        кликов капитана, которые могли бы оба "успеть" выбрать одного и
        того же игрока до того, как первый пик уберёт его из доступных.

        Возвращает True при успехе, False — если игрока уже кто-то забрал
        (в паре с asyncio.Lock в DraftView это практически невозможный,
        но всё равно подстрахованный случай)."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE game_players SET team = ? WHERE game_id = ? AND player_id = ? AND team IS NULL",
            (team, game_id, player_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def get_team_players(self, game_id: int, team: str) -> list[User]:
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT users.* FROM game_players
            JOIN users ON users.id = game_players.player_id
            WHERE game_players.game_id = ? AND game_players.team = ?
            ORDER BY game_players.captain DESC, game_players.rowid ASC
            """,
            (game_id, team),
        ) as cursor:
            rows = await cursor.fetchall()
        return [User.from_row(row) for row in rows]

    async def get_available_players(self, game_id: int) -> list[User]:
        """Игроки набора, которые ещё не в команде (не капитаны и не выбраны)."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT users.* FROM game_players
            JOIN users ON users.id = game_players.player_id
            WHERE game_players.game_id = ? AND game_players.team IS NULL
            ORDER BY game_players.rowid ASC
            """,
            (game_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [User.from_row(row) for row in rows]

    # --- Голосовые каналы (Этап 13-15) ----------------------------------

    @_serialized_write
    async def set_voice_channels(self, game_id: int, blue_voice_channel_id: Optional[int], red_voice_channel_id: Optional[int]) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE games SET blue_voice_channel_id = ?, red_voice_channel_id = ? WHERE game_id = ?",
            (blue_voice_channel_id, red_voice_channel_id, game_id),
        )
        await self._conn.commit()

    @_serialized_write
    async def clear_voice_channels(self, game_id: int, clear_blue: bool = True, clear_red: bool = True) -> None:
        """Fix #7: обнуляет ссылки на голосовые каналы после того, как они
        реально удалены — чтобы cleanup-проверка при следующем рестарте не
        считала их "оставшимися" повторно.

        Fix (round 3, критический — требование п.4 ревью): раньше метод
        ВСЕГДА обнулял ОБА столбца, даже если реально подтверждено удаление
        только одного канала. Если, например, синий канал удалился успешно,
        а красный упал с HTTPException (не "канала уже нет", а настоящая
        ошибка Discord API), вызывающий код раньше всё равно звал
        clear_voice_channels(game_id) целиком — БД теряла единственную
        ссылку на всё ещё существующий красный канал, и он оставался в
        гильдии навсегда (cleanup_stale_voice_channels никогда бы его не
        нашёл, потому что запрос ищет игры, где ID ещё сохранён).

        Теперь вызывающий код обязан явно указать, какие из двух каналов
        подтверждённо обработаны (удалены или точно уже не существуют), а
        какие — нет; необработанный столбец не трогаем, БД продолжает
        отражать фактическое состояние Discord."""
        assert self._conn is not None
        if not clear_blue and not clear_red:
            return
        columns = []
        if clear_blue:
            columns.append("blue_voice_channel_id = NULL")
        if clear_red:
            columns.append("red_voice_channel_id = NULL")
        await self._conn.execute(
            f"UPDATE games SET {', '.join(columns)} WHERE game_id = ?", (game_id,)
        )
        await self._conn.commit()

    async def get_games_with_leftover_voice_channels(self) -> list[Game]:
        """Fix #7: игры, которые уже COMPLETED/CANCELLED, но у которых
        всё ещё сохранены ID голосовых каналов — значит, штатное удаление
        (через asyncio.create_task с задержкой) не успело выполниться до
        остановки/перезапуска бота, и каналы могли остаться в гильдии
        навсегда. Проверяется один раз при старте бота."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM games WHERE status IN ('COMPLETED', 'CANCELLED') "
            "AND (blue_voice_channel_id IS NOT NULL OR red_voice_channel_id IS NOT NULL)"
        ) as cursor:
            rows = await cursor.fetchall()
        return [Game.from_row(row) for row in rows]

    # --- Результаты и ELO (Этапы 23-30) ---------------------------------

    async def get_user_by_id(self, user_id: int) -> Optional[User]:
        assert self._conn is not None
        async with self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
        return User.from_row(row) if row else None

    @_serialized_write
    async def set_screenshot(self, game_id: int, message_id: int) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE games SET screenshot_message_id = ? WHERE game_id = ?", (message_id, game_id)
        )
        await self._conn.commit()

    @_serialized_write
    async def submit_screenshot_atomic(self, game_id: int, message_id: int) -> bool:
        """Fix (round 2, #1 — критический): атомарно фиксирует скриншот и
        переводит игру PLAYING -> PENDING_CONFIRMATION ОДНИМ SQL-выражением.

        Раньше это были два отдельных запроса (set_screenshot +
        update_game_status), и если два игрока отправляли скриншот
        практически одновременно, оба проходили проверку "статус == PLAYING"
        до того, как первый успевал переключить статус — в БД мог
        оказаться скриншот от второго игрока, а первый запрос на
        подтверждение уже улетел администратору с другим screenshot_message_id.

        WHERE status = 'PLAYING' гарантирует, что только ОДИН вызов
        (тот, что физически выполнится в БД первым) реально сменит статус —
        у второго rowcount будет 0, и мы вернём False."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE games SET screenshot_message_id = ?, status = 'PENDING_CONFIRMATION' "
            "WHERE game_id = ? AND status = 'PLAYING'",
            (message_id, game_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    @_serialized_write
    async def try_confirm_winner(self, game_id: int, winner: str, confirmed_by: int) -> bool:
        """Атомарно фиксирует победителя (защита от двойного начисления, ТЗ п.30).

        Условие WHERE ... AND winner IS NULL гарантирует, что при гонке двух
        одновременных нажатий (например, два администратора кликнули почти
        одновременно) UPDATE применится только у одного из них — у второго
        rowcount будет 0, и мы вернём False, ELO при этом начислен не будет.
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            UPDATE games
            SET winner = ?, result_confirmed_by = ?, result_confirmed_at = ?,
                status = 'COMPLETED', finished_at = ?
            WHERE game_id = ? AND status = 'PENDING_CONFIRMATION' AND winner IS NULL
            """,
            (winner, confirmed_by, _utcnow(), _utcnow(), game_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def _apply_result_statements(
        self,
        game_id: int,
        player_id: int,
        elo_delta: int,
        result: str,
        min_elo: int,
        reason: str = "game_result",
        protection_used: bool = False,
    ) -> int:
        """Та же логика, что и apply_game_result, но БЕЗ commit() — строительный
        блок внутри атомарных транзакций. reason попадает в elo_history
        (например game_result / elo_protection), чтобы не плодить дубликаты."""
        assert self._conn is not None
        user = await self.get_user_by_id(player_id)
        assert user is not None

        old_elo = user.elo
        new_elo = max(min_elo, old_elo + elo_delta)
        actual_change = new_elo - old_elo

        win_inc = 1 if result == "WIN" else 0
        loss_inc = 1 if result == "LOSS" else 0

        await self._conn.execute(
            "UPDATE users SET elo = ?, wins = wins + ?, losses = losses + ?, games_played = games_played + 1 "
            "WHERE id = ?",
            (new_elo, win_inc, loss_inc, player_id),
        )
        await self._conn.execute(
            "UPDATE game_players SET result = ?, elo_change = ?, protection_used = ? "
            "WHERE game_id = ? AND player_id = ?",
            (result, actual_change, 1 if protection_used else 0, game_id, player_id),
        )
        await self._conn.execute(
            "INSERT INTO elo_history (player_id, game_id, old_elo, elo_change, new_elo, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (player_id, game_id, old_elo, actual_change, new_elo, reason, _utcnow()),
        )
        return actual_change

    @_serialized_write
    async def apply_game_result(self, game_id: int, player_id: int, elo_delta: int, result: str, min_elo: int = 0) -> int:
        """Обновляет ELO/статистику одного игрока по итогам игры и сразу
        коммитит. Используется там, где применяется изменение только
        ОДНОГО игрока за раз вне более крупной транзакции.

        ELO не может уйти ниже min_elo (по умолчанию 0, см. ТЗ п.27) — если
        начисление привело бы к меньшему значению, применяется меньшее
        фактическое изменение, и именно оно (а не "сырые" +25/-25) пишется
        в game_players и elo_history, чтобы история была правдивой.

        Возвращает фактически применённое изменение ELO.
        """
        assert self._conn is not None
        actual_change = await self._apply_result_statements(game_id, player_id, elo_delta, result, min_elo)
        await self._conn.commit()
        return actual_change

    @_serialized_write
    async def finalize_game_result_atomic(
        self,
        game_id: int,
        winner: str,
        confirmed_by: int,
        winner_ids: list[int],
        loser_ids: list[int],
        elo_win: int,
        elo_loss: int,
        min_elo: int = 0,
        win_vc_map: dict[int, int] | None = None,
        loss_vc_map: dict[int, int] | None = None,
    ) -> bool:
        """Фиксация победителя + ELO + Elo Protection + VC в ОДНОЙ транзакции.

        Idempotency: WHERE status=PENDING_CONFIRMATION AND winner IS NULL —
        повторное подтверждение не начислит ELO/VC повторно.
        MATCH_WIN/MATCH_LOSS также защищены unique-индексом по (user, game, type).
        """
        assert self._conn is not None
        win_vc_map = win_vc_map or {}
        loss_vc_map = loss_vc_map or {}
        try:
            cursor = await self._conn.execute(
                """
                UPDATE games
                SET winner = ?, result_confirmed_by = ?, result_confirmed_at = ?,
                    status = 'COMPLETED', finished_at = ?
                WHERE game_id = ? AND status = 'PENDING_CONFIRMATION' AND winner IS NULL
                """,
                (winner, confirmed_by, _utcnow(), _utcnow(), game_id),
            )
            if cursor.rowcount == 0:
                await self._conn.rollback()
                return False

            for player_id in winner_ids:
                await self._apply_result_statements(game_id, player_id, elo_win, "WIN", min_elo)
                vc = int(win_vc_map.get(player_id, 0))
                if vc > 0:
                    await self._credit_vc_unlocked(
                        player_id, vc, "MATCH_WIN", f"Победа в игре #{game_id}", game_id
                    )

            for player_id in loser_ids:
                user = await self.get_user_by_id(player_id)
                assert user is not None
                protected = user.elo_protection > 0
                effective_loss = 0 if protected else elo_loss
                if protected:
                    dec_cursor = await self._conn.execute(
                        "UPDATE users SET elo_protection = elo_protection - 1 "
                        "WHERE id = ? AND elo_protection > 0",
                        (player_id,),
                    )
                    # BUG #17 fix: if the CAS'd decrement didn't actually hit
                    # a row (protection got consumed by something else
                    # between the read above and this UPDATE — only
                    # theoretically possible today since the whole
                    # transaction is under the write lock, but this keeps
                    # `protected` truthful if that ever changes), treat the
                    # player as unprotected for the ELO/loss calc below
                    # rather than recording protection_used=1 while no
                    # protection charge actually happened.
                    if dec_cursor.rowcount == 0:
                        protected = False
                        effective_loss = elo_loss
                await self._apply_result_statements(
                    game_id,
                    player_id,
                    effective_loss,
                    "LOSS",
                    min_elo,
                    reason="elo_protection" if protected else "game_result",
                    protection_used=protected,
                )
                vc = int(loss_vc_map.get(player_id, 0))
                if vc > 0:
                    await self._credit_vc_unlocked(
                        player_id, vc, "MATCH_LOSS", f"Участие в игре #{game_id}", game_id
                    )

            await self._conn.commit()
            return True
        except Exception:
            log.exception("Ошибка внутри атомарной транзакции завершения игры #%s — откат (ROLLBACK)", game_id)
            await self._conn.rollback()
            raise

    async def _credit_vc_unlocked(
        self,
        user_id: int,
        amount: int,
        tx_type: str,
        description: str | None = None,
        game_id: int | None = None,
    ) -> int:
        """Начисление VC без commit (только внутри уже открытой транзакции)."""
        assert self._conn is not None
        if amount <= 0:
            raise ValueError("credit amount must be positive")
        await self._conn.execute(
            "UPDATE users SET vc_balance = vc_balance + ? WHERE id = ?",
            (amount, user_id),
        )
        async with self._conn.execute(
            "SELECT vc_balance FROM users WHERE id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        balance = int(row["vc_balance"])
        await self._conn.execute(
            """
            INSERT INTO coin_transactions
                (user_id, amount, balance_after, transaction_type, description, game_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, amount, balance, tx_type, description, game_id, _utcnow()),
        )
        return balance

    async def _debit_vc_unlocked(
        self,
        user_id: int,
        amount: int,
        tx_type: str,
        description: str | None = None,
        game_id: int | None = None,
    ) -> int:
        """Списание VC без commit. Не уходит в минус — при нехватке Integrity-подобная ошибка через rowcount."""
        assert self._conn is not None
        if amount <= 0:
            raise ValueError("debit amount must be positive")
        cursor = await self._conn.execute(
            "UPDATE users SET vc_balance = vc_balance - ? "
            "WHERE id = ? AND vc_balance >= ?",
            (amount, user_id, amount),
        )
        if cursor.rowcount == 0:
            raise ValueError("insufficient_vc")
        async with self._conn.execute(
            "SELECT vc_balance FROM users WHERE id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        balance = int(row["vc_balance"])
        await self._conn.execute(
            """
            INSERT INTO coin_transactions
                (user_id, amount, balance_after, transaction_type, description, game_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, -amount, balance, tx_type, description, game_id, _utcnow()),
        )
        return balance

    @_serialized_write
    async def edit_game_result_atomic(
        self,
        game_id: int,
        expected_current_winner: str,
        new_winner: str,
        confirmed_by: int,
        new_winner_ids: list[int],
        new_loser_ids: list[int],
        elo_win: int,
        elo_loss: int,
        min_elo: int = 0,
        win_vc_map: dict[int, int] | None = None,
        loss_vc_map: dict[int, int] | None = None,
    ) -> bool:
        """Откат ELO + VC + Elo Protection, затем пересчёт под нового победителя.

        VC матча реверсируются по coin_transactions (MATCH_*), protection
        восстанавливается из game_players.protection_used, затем применяется
        та же логика, что и при первичном finalize (включая protection).
        """
        assert self._conn is not None
        win_vc_map = win_vc_map or {}
        loss_vc_map = loss_vc_map or {}
        try:
            cas_cursor = await self._conn.execute(
                "UPDATE games SET winner = ? WHERE game_id = ? AND status = 'COMPLETED' AND winner = ?",
                (new_winner, game_id, expected_current_winner),
            )
            if cas_cursor.rowcount == 0:
                await self._conn.rollback()
                return False

            # --- 1) Revert match VC ---
            async with self._conn.execute(
                "SELECT id, user_id, amount FROM coin_transactions "
                "WHERE game_id = ? AND transaction_type IN ('MATCH_WIN', 'MATCH_LOSS')",
                (game_id,),
            ) as cursor:
                vc_rows = await cursor.fetchall()
            for row in vc_rows:
                amount = int(row["amount"])
                uid = int(row["user_id"])
                # amount was credited (positive); reverse only what is still there.
                # Player may have spent the reward in shop/casino — never go negative.
                async with self._conn.execute(
                    "SELECT vc_balance FROM users WHERE id = ?", (uid,)
                ) as c_before:
                    bal_before = int((await c_before.fetchone())["vc_balance"])
                actual_reversed = min(amount, max(0, bal_before))
                await self._conn.execute(
                    "UPDATE users SET vc_balance = MAX(0, vc_balance - ?) WHERE id = ?",
                    (amount, uid),
                )
                async with self._conn.execute(
                    "SELECT vc_balance FROM users WHERE id = ?", (uid,)
                ) as c2:
                    bal = int((await c2.fetchone())["vc_balance"])
                if actual_reversed > 0:
                    await self._conn.execute(
                        """
                        INSERT INTO coin_transactions
                            (user_id, amount, balance_after, transaction_type, description, game_id, created_at)
                        VALUES (?, ?, ?, 'REFUND', ?, ?, ?)
                        """,
                        (
                            uid,
                            -actual_reversed,
                            bal,
                            f"Откат VC матча #{game_id} (edit_result, clawed {actual_reversed}/{amount})",
                            game_id,
                            _utcnow(),
                        ),
                    )
            # Не удаляем оригинальные MATCH_WIN/MATCH_LOSS — переименовываем type
            # в *_REVERSED, чтобы сохранить аудируемость ledger (BUG #14 fix) и
            # одновременно освободить unique index (user_id, game_id, type) для
            # новых MATCH_WIN/MATCH_LOSS строк, которые вставляются ниже при
            # переприменении результата.
            await self._conn.execute(
                "UPDATE coin_transactions SET "
                "transaction_type = transaction_type || '_REVERSED', "
                "description = description || ' [REVERSED edit_result]' "
                "WHERE game_id = ? AND transaction_type IN ('MATCH_WIN', 'MATCH_LOSS')",
                (game_id,),
            )

            # --- 2) Restore protection consumed on original loss ---
            async with self._conn.execute(
                "SELECT player_id FROM game_players WHERE game_id = ? AND protection_used = 1",
                (game_id,),
            ) as cursor:
                prot_rows = await cursor.fetchall()
            for row in prot_rows:
                await self._conn.execute(
                    "UPDATE users SET elo_protection = elo_protection + 1 WHERE id = ?",
                    (row["player_id"],),
                )

            # --- 3) Revert ELO / W-L stats ---
            async with self._conn.execute(
                "SELECT player_id, result, elo_change FROM game_players "
                "WHERE game_id = ? AND result IS NOT NULL",
                (game_id,),
            ) as cursor:
                rows = await cursor.fetchall()

            for row in rows:
                player_id, result, elo_change = row["player_id"], row["result"], row["elo_change"]
                if elo_change is None:
                    continue
                user = await self.get_user_by_id(player_id)
                assert user is not None
                old_elo = user.elo
                new_elo = old_elo - elo_change
                win_dec = 1 if result == "WIN" else 0
                loss_dec = 1 if result == "LOSS" else 0
                # Floor stats at 0 — never let a double-revert or concurrent
                # admin tool drive wins/losses/games_played negative.
                await self._conn.execute(
                    "UPDATE users SET elo = ?, "
                    "wins = MAX(0, wins - ?), losses = MAX(0, losses - ?), "
                    "games_played = MAX(0, games_played - 1) WHERE id = ?",
                    (new_elo, win_dec, loss_dec, player_id),
                )
                await self._conn.execute(
                    "INSERT INTO elo_history (player_id, game_id, old_elo, elo_change, new_elo, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'result_edit_revert', ?)",
                    (player_id, game_id, old_elo, -elo_change, new_elo, _utcnow()),
                )
                await self._conn.execute(
                    "UPDATE game_players SET result = NULL, elo_change = NULL, protection_used = 0 "
                    "WHERE game_id = ? AND player_id = ?",
                    (game_id, player_id),
                )

            # --- 4) Re-apply with protection + new VC ---
            for player_id in new_winner_ids:
                await self._apply_result_statements(game_id, player_id, elo_win, "WIN", min_elo)
                vc = int(win_vc_map.get(player_id, 0))
                if vc > 0:
                    await self._credit_vc_unlocked(
                        player_id, vc, "MATCH_WIN", f"Победа в игре #{game_id} (edit)", game_id
                    )

            for player_id in new_loser_ids:
                user = await self.get_user_by_id(player_id)
                assert user is not None
                protected = user.elo_protection > 0
                effective_loss = 0 if protected else elo_loss
                if protected:
                    dec_cursor = await self._conn.execute(
                        "UPDATE users SET elo_protection = elo_protection - 1 "
                        "WHERE id = ? AND elo_protection > 0",
                        (player_id,),
                    )
                    # BUG #17 fix, same as finalize_game_result_atomic.
                    if dec_cursor.rowcount == 0:
                        protected = False
                        effective_loss = elo_loss
                await self._apply_result_statements(
                    game_id,
                    player_id,
                    effective_loss,
                    "LOSS",
                    min_elo,
                    reason="elo_protection" if protected else "game_result",
                    protection_used=protected,
                )
                vc = int(loss_vc_map.get(player_id, 0))
                if vc > 0:
                    await self._credit_vc_unlocked(
                        player_id, vc, "MATCH_LOSS", f"Участие в игре #{game_id} (edit)", game_id
                    )

            await self._conn.execute(
                "UPDATE games SET winner = ?, result_confirmed_by = ?, result_confirmed_at = ? WHERE game_id = ?",
                (new_winner, confirmed_by, _utcnow(), game_id),
            )
            await self._conn.commit()
            return True
        except Exception:
            log.exception("Ошибка внутри атомарного исправления результата игры #%s — откат (ROLLBACK)", game_id)
            await self._conn.rollback()
            raise

    async def get_game_result_details(
        self, game_id: int
    ) -> dict[str, list[tuple[User, int, bool]]]:
        """Итоговый состав команд с elo_change и фактом срабатывания защиты.

        Третий элемент кортежа — ``protection_used`` из ``game_players``
        (защита реально сработала в этом матче), а не текущий инвентарь
        ``users.elo_protection``.
        """
        assert self._conn is not None
        details: dict[str, list[tuple[User, int, bool]]] = {"BLUE": [], "RED": []}
        async with self._conn.execute(
            """
            SELECT users.*, game_players.team AS gp_team,
                   game_players.elo_change AS gp_elo_change,
                   game_players.protection_used AS gp_protection_used
            FROM game_players JOIN users ON users.id = game_players.player_id
            WHERE game_players.game_id = ?
            ORDER BY game_players.captain DESC, game_players.rowid ASC
            """,
            (game_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            team = row["gp_team"]
            if team in details:
                protection_used = bool(row["gp_protection_used"] or 0)
                details[team].append(
                    (User.from_row(row), row["gp_elo_change"], protection_used)
                )
        return details

    # --- Исправление результата (Этап 31) --------------------------------

    @_serialized_write
    async def revert_game_result(self, game_id: int) -> None:
        """Откатывает ELO/статистику, применённые этой игрой, используя
        фактически сохранённые result/elo_change из game_players — то есть
        отменяется именно то, что было реально начислено (с учётом clamp
        на 0), а не "сырые" +25/-25. Каждый откат пишется в elo_history
        отдельной строкой с reason='result_edit_revert', чтобы история
        осталась честной и прослеживаемой (п.29, п.31)."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT player_id, result, elo_change FROM game_players "
            "WHERE game_id = ? AND result IS NOT NULL",
            (game_id,),
        ) as cursor:
            rows = await cursor.fetchall()

        for row in rows:
            player_id, result, elo_change = row["player_id"], row["result"], row["elo_change"]
            if elo_change is None:
                continue

            user = await self.get_user_by_id(player_id)
            assert user is not None
            old_elo = user.elo
            new_elo = old_elo - elo_change  # точный откат фактически применённого изменения

            win_dec = 1 if result == "WIN" else 0
            loss_dec = 1 if result == "LOSS" else 0

            await self._conn.execute(
                "UPDATE users SET elo = ?, "
                "wins = MAX(0, wins - ?), losses = MAX(0, losses - ?), "
                "games_played = MAX(0, games_played - 1) WHERE id = ?",
                (new_elo, win_dec, loss_dec, player_id),
            )
            await self._conn.execute(
                "INSERT INTO elo_history (player_id, game_id, old_elo, elo_change, new_elo, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'result_edit_revert', ?)",
                (player_id, game_id, old_elo, -elo_change, new_elo, _utcnow()),
            )
            await self._conn.execute(
                "UPDATE game_players SET result = NULL, elo_change = NULL WHERE game_id = ? AND player_id = ?",
                (game_id, player_id),
            )
        await self._conn.commit()

    @_serialized_write
    async def update_game_winner(self, game_id: int, winner: str, confirmed_by: int) -> None:
        """Перезаписывает победителя уже COMPLETED-игры (используется
        только вместе с revert_game_result + повторным apply_game_result,
        никогда отдельно — иначе ELO и games.winner разойдутся)."""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE games SET winner = ?, result_confirmed_by = ?, result_confirmed_at = ? WHERE game_id = ?",
            (winner, confirmed_by, _utcnow(), game_id),
        )
        await self._conn.commit()

    # --- История матчей (Этап 34) ---------------------------------------

    async def get_player_matches(
        self, player_internal_id: int, limit: int = 10, offset: int = 0
    ) -> list[MatchRecord]:
        """История завершённых игр конкретного игрока, от новых к старым."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT games.*, gp.team AS gp_team, gp.captain AS gp_captain,
                   gp.result AS gp_result, gp.elo_change AS gp_elo_change,
                   bc.discord_username AS blue_captain_name,
                   rc.discord_username AS red_captain_name
            FROM game_players gp
            JOIN games ON games.game_id = gp.game_id
            LEFT JOIN users bc ON bc.id = games.blue_captain
            LEFT JOIN users rc ON rc.id = games.red_captain
            WHERE gp.player_id = ? AND games.status = 'COMPLETED'
            ORDER BY games.finished_at DESC
            LIMIT ? OFFSET ?
            """,
            (player_internal_id, limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
        return [MatchRecord.from_row(row) for row in rows]

    async def count_player_matches(self, player_internal_id: int) -> int:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) AS count FROM game_players gp JOIN games g ON g.game_id = gp.game_id "
            "WHERE gp.player_id = ? AND g.status = 'COMPLETED'",
            (player_internal_id,),
        ) as cursor:
            return (await cursor.fetchone())["count"]

    async def get_match_details(self, game_id: int) -> tuple[Game, list[tuple[User, str | None, bool, str | None, int | None]]] | None:
        """Полное восстановление завершённого матча из games + game_players."""
        game = await self.get_game(game_id)
        if game is None:
            return None
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT users.*, gp.team AS gp_team, gp.captain AS gp_captain,
                   gp.result AS gp_result, gp.elo_change AS gp_elo_change
            FROM game_players gp JOIN users ON users.id = gp.player_id
            WHERE gp.game_id = ? ORDER BY gp.captain DESC, gp.rowid ASC
            """,
            (game_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return game, [
            (User.from_row(row), row["gp_team"], bool(row["gp_captain"]), row["gp_result"], row["gp_elo_change"])
            for row in rows
        ]

    async def has_later_completed_game(self, game_id: int) -> bool:
        """BUG #15: True if any participant of this game already has a
        COMPLETED match with a higher game_id (i.e. played after this one).

        Editing an old result without replaying every subsequent
        elo_history row would leave intermediate ratings inconsistent.
        Callers of /edit_result must refuse when this returns True.
        """
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT 1
            FROM game_players gp_old
            JOIN game_players gp_later ON gp_later.player_id = gp_old.player_id
            JOIN games g_later ON g_later.game_id = gp_later.game_id
            WHERE gp_old.game_id = ?
              AND g_later.status = 'COMPLETED'
              AND g_later.game_id > ?
            LIMIT 1
            """,
            (game_id, game_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    # --- Persistent matchmaking queue ----------------------------------

    async def get_queue_entries(self, guild_id: int) -> list[QueueEntry]:
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT queue_entries.guild_id AS queue_guild_id, queue_entries.queued_at, users.*
            FROM queue_entries JOIN users ON users.id = queue_entries.player_id
            WHERE queue_entries.guild_id = ?
            ORDER BY queue_entries.queued_at, queue_entries.player_id
            """,
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [QueueEntry.from_row(row) for row in rows]

    async def is_player_in_active_game(self, guild_id: int, player_id: int) -> bool:
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT 1 FROM game_players gp JOIN games g ON g.game_id = gp.game_id
            WHERE g.guild_id = ? AND gp.player_id = ?
              AND g.status NOT IN ('COMPLETED', 'CANCELLED') LIMIT 1
            """,
            (guild_id, player_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    @_serialized_write
    async def join_queue(self, guild_id: int, player_id: int) -> tuple[bool, int, int]:
        """Добавляет игрока в очередь. Возвращает (added, total, position).

        BUG #13 fix: the "not already in an active game" check used to live
        only in the cog, as a separate SELECT before this INSERT — a lobby
        join and a queue join for the same player could race right between
        that check and the write on either side, landing the player in both
        places at once. The active-game check is now folded into the same
        atomic INSERT...SELECT as the queue insert itself.
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO queue_entries(guild_id, player_id, queued_at)
            SELECT ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM queue_entries WHERE guild_id = ? AND player_id = ?
            )
            AND NOT EXISTS (
                SELECT 1 FROM game_players gp JOIN games g ON g.game_id = gp.game_id
                WHERE g.guild_id = ? AND gp.player_id = ?
                  AND g.status NOT IN ('COMPLETED', 'CANCELLED')
            )
            """,
            (guild_id, player_id, _utcnow(), guild_id, player_id, guild_id, player_id),
        )
        added = cursor.rowcount > 0
        async with self._conn.execute(
            "SELECT COUNT(*) AS count FROM queue_entries WHERE guild_id = ?", (guild_id,)
        ) as count_cursor:
            total = (await count_cursor.fetchone())["count"]
        async with self._conn.execute(
            """
            SELECT COUNT(*) AS count FROM queue_entries
            WHERE guild_id = ? AND (queued_at < (SELECT queued_at FROM queue_entries WHERE guild_id = ? AND player_id = ?)
              OR (queued_at = (SELECT queued_at FROM queue_entries WHERE guild_id = ? AND player_id = ?) AND player_id <= ?))
            """,
            (guild_id, guild_id, player_id, guild_id, player_id, player_id),
        ) as position_cursor:
            position = (await position_cursor.fetchone())["count"]
        await self._conn.commit()
        return added, total, position

    @_serialized_write
    async def leave_queue(self, guild_id: int, player_id: int) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "DELETE FROM queue_entries WHERE guild_id = ? AND player_id = ?", (guild_id, player_id)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    @_serialized_write
    async def cancel_and_requeue_atomic(self, game_id: int, guild_id: int, player_ids: list[int]) -> bool:
        """Fix (аудит, критический — п.2/п.11 ревью): отменяет ещё не
        анонсированную игру и одной транзакцией возвращает её участников
        в очередь.

        claim_queue_match атомарно снимает игроков с очереди и создаёт
        Game ДО того, как матч анонсирован в Discord (embed отправлен,
        lobby_message_id/draft_message_id сохранены). Если отправка
        объявления или что-то на пути к нему падает (discord.HTTPException,
        канал недоступен, ошибка БД), раньше вызывающий код просто
        отменял игру (cancel_active_game_atomic) и возвращал управление —
        игроки при этом "исчезали": они уже не в очереди (claim_queue_match
        их удалил) и не в игре (она CANCELLED), заново вставать в очередь
        нужно было вручную. Эта функция закрывает то же окно, что описано
        в ревью про claim_queue_match → announce → set_lobby_message:
        отмена и возврат в очередь происходят как одна атомарная операция,
        так что промежуточного состояния "игроков нет нигде" не существует
        ни при каком crash между шагами внутри неё."""
        assert self._conn is not None
        try:
            cursor = await self._conn.execute(
                "UPDATE games SET status = 'CANCELLED', finished_at = ? "
                "WHERE game_id = ? AND status NOT IN ('COMPLETED', 'CANCELLED')",
                (_utcnow(), game_id),
            )
            cancelled = cursor.rowcount > 0
            if cancelled and player_ids:
                await self._conn.executemany(
                    "INSERT OR IGNORE INTO queue_entries(guild_id, player_id, queued_at) VALUES (?, ?, ?)",
                    [(guild_id, pid, _utcnow()) for pid in player_ids],
                )
            await self._conn.commit()
            return cancelled
        except Exception:
            await self._conn.rollback()
            log.exception("Не удалось отменить и вернуть в очередь игру #%s", game_id)
            raise

    @_serialized_write
    async def claim_queue_match(self, guild_id: int, max_players: int) -> tuple[Game | None, list[User]]:
        """Атомарно забирает первые N игроков и создаёт один обычный Game.

        При любой ошибке транзакция откатывается: записи очереди остаются,
        поэтому игроки не исчезают при сбое создания матча.
        """
        assert self._conn is not None
        try:
            if await self.get_active_game(guild_id) is not None:
                return None, []
            async with self._conn.execute(
                """
                SELECT users.* FROM queue_entries JOIN users ON users.id = queue_entries.player_id
                WHERE queue_entries.guild_id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM game_players gp JOIN games g ON g.game_id = gp.game_id
                    WHERE gp.player_id = users.id AND g.guild_id = ?
                      AND g.status NOT IN ('COMPLETED', 'CANCELLED')
                  )
                ORDER BY queue_entries.queued_at, queue_entries.player_id LIMIT ?
                """,
                (guild_id, guild_id, max_players),
            ) as cursor:
                players = [User.from_row(row) for row in await cursor.fetchall()]
            if len(players) < max_players:
                return None, []

            cursor = await self._conn.execute(
                "INSERT INTO games(guild_id, status, created_at) VALUES (?, 'WAITING', ?)",
                (guild_id, _utcnow()),
            )
            game_id = cursor.lastrowid
            for player in players:
                await self._conn.execute(
                    "INSERT INTO game_players(game_id, player_id, captain) VALUES (?, ?, 0)",
                    (game_id, player.id),
                )
            await self._conn.executemany(
                "DELETE FROM queue_entries WHERE guild_id = ? AND player_id = ?",
                [(guild_id, player.id) for player in players],
            )
            await self._conn.commit()
            game = await self.get_game(game_id)
            assert game is not None
            return game, players
        except Exception:
            await self._conn.rollback()
            log.exception("Не удалось создать матч из очереди сервера %s", guild_id)
            raise


    # --- Valhalla Coin / Economy -----------------------------------------

    @_serialized_write
    async def credit_vc(
        self,
        user_id: int,
        amount: int,
        tx_type: str,
        description: str | None = None,
        game_id: int | None = None,
    ) -> int:
        try:
            balance = await self._credit_vc_unlocked(user_id, amount, tx_type, description, game_id)
            await self._conn.commit()
            return balance
        except Exception:
            await self._conn.rollback()
            raise

    @_serialized_write
    async def debit_vc(
        self,
        user_id: int,
        amount: int,
        tx_type: str,
        description: str | None = None,
        game_id: int | None = None,
    ) -> int:
        try:
            balance = await self._debit_vc_unlocked(user_id, amount, tx_type, description, game_id)
            await self._conn.commit()
            return balance
        except Exception:
            await self._conn.rollback()
            raise

    @_serialized_write
    async def set_vc_balance(self, user_id: int, new_balance: int, admin_note: str) -> int:
        if new_balance < 0:
            raise ValueError("balance cannot be negative")
        assert self._conn is not None
        try:
            async with self._conn.execute(
                "SELECT vc_balance FROM users WHERE id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise ValueError("user not found")
            old = int(row["vc_balance"])
            delta = new_balance - old
            await self._conn.execute(
                "UPDATE users SET vc_balance = ? WHERE id = ?", (new_balance, user_id)
            )
            tx_type = "ADMIN_GRANT" if delta >= 0 else "ADMIN_REMOVE"
            await self._conn.execute(
                """
                INSERT INTO coin_transactions
                    (user_id, amount, balance_after, transaction_type, description, game_id, created_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (user_id, delta, new_balance, tx_type, admin_note, _utcnow()),
            )
            await self._conn.commit()
            return new_balance
        except Exception:
            await self._conn.rollback()
            raise

    async def get_coin_transactions(self, user_id: int, limit: int = 20) -> list:
        from database.models import CoinTransaction

        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM coin_transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [CoinTransaction.from_row(r) for r in rows]

    @_serialized_write
    async def add_elo_protection(self, user_id: int, quantity: int = 1) -> int:
        assert self._conn is not None
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        await self._conn.execute(
            "UPDATE users SET elo_protection = elo_protection + ? WHERE id = ?",
            (quantity, user_id),
        )
        async with self._conn.execute(
            "SELECT elo_protection FROM users WHERE id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        await self._conn.commit()
        return int(row["elo_protection"])

    @_serialized_write
    async def purchase_elo_protection(self, user_id: int, price: int) -> tuple[int, int]:
        """Атомарно: списать VC + выдать 1 protection. Возвращает (vc_balance, protection)."""
        assert self._conn is not None
        try:
            await self._debit_vc_unlocked(user_id, price, "SHOP_PURCHASE", "Покупка Elo Protection")
            await self._conn.execute(
                "UPDATE users SET elo_protection = elo_protection + 1 WHERE id = ?",
                (user_id,),
            )
            async with self._conn.execute(
                "SELECT vc_balance, elo_protection FROM users WHERE id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
            await self._conn.commit()
            return int(row["vc_balance"]), int(row["elo_protection"])
        except Exception:
            await self._conn.rollback()
            raise

    @_serialized_write
    async def admin_adjust_elo(
        self, user_id: int, delta: int, admin_discord_id: int, reason: str
    ) -> tuple[int, int]:
        """BUG #12 fix: atomic ELO +/- delta from the CURRENT value, read
        under the same write lock as the update. /admin_elo add and
        /admin_elo remove used to read `user.elo` in the cog (outside any
        lock), compute new_elo = old + amount there, and pass the already
        stale absolute value to admin_set_elo — two admins adjusting the
        same player around the same time could silently lose one of the
        two deltas. Doing the read-modify-write here, inside the lock,
        makes concurrent adjustments commute correctly."""
        assert self._conn is not None
        try:
            user = await self.get_user_by_id(user_id)
            if user is None:
                raise ValueError("user not found")
            old = user.elo
            new_elo = max(0, old + delta)
            actual_change = new_elo - old
            await self._conn.execute(
                "UPDATE users SET elo = ? WHERE id = ?", (new_elo, user_id)
            )
            await self._conn.execute(
                "INSERT INTO elo_history "
                "(player_id, game_id, old_elo, elo_change, new_elo, reason, created_at) "
                "VALUES (?, NULL, ?, ?, ?, ?, ?)",
                (user_id, old, actual_change, new_elo, f"admin:{admin_discord_id}:{reason}", _utcnow()),
            )
            await self._conn.commit()
            return old, new_elo
        except Exception:
            await self._conn.rollback()
            raise

    @_serialized_write
    async def admin_set_elo(
        self, user_id: int, new_elo: int, admin_discord_id: int, reason: str
    ) -> tuple[int, int]:
        if new_elo < 0:
            raise ValueError("elo cannot be negative")
        assert self._conn is not None
        try:
            user = await self.get_user_by_id(user_id)
            if user is None:
                raise ValueError("user not found")
            old = user.elo
            change = new_elo - old
            await self._conn.execute(
                "UPDATE users SET elo = ? WHERE id = ?", (new_elo, user_id)
            )
            await self._conn.execute(
                "INSERT INTO elo_history "
                "(player_id, game_id, old_elo, elo_change, new_elo, reason, created_at) "
                "VALUES (?, NULL, ?, ?, ?, ?, ?)",
                (user_id, old, change, new_elo, f"admin:{admin_discord_id}:{reason}", _utcnow()),
            )
            await self._conn.commit()
            return old, new_elo
        except Exception:
            await self._conn.rollback()
            raise

    @_serialized_write
    async def play_casino_slots(
        self,
        user_id: int,
        bet: int,
        symbols: tuple[str, str, str],
        payout: int,
        result_label: str,
        interaction_id: str | None = None,
    ) -> tuple[int, int]:
        """Атомарно: ставка + выплата. interaction_id — идемпотентность Discord interaction."""
        assert self._conn is not None
        if bet <= 0:
            raise ValueError("bet must be positive")
        try:
            if interaction_id:
                async with self._conn.execute(
                    "SELECT 1 FROM casino_games WHERE interaction_id = ?",
                    (interaction_id,),
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        raise ValueError("duplicate_interaction")

            async with self._conn.execute(
                "SELECT vc_balance FROM users WHERE id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise ValueError("user not found")
            balance_before = int(row["vc_balance"])
            if balance_before < bet:
                raise ValueError("insufficient_vc")

            await self._debit_vc_unlocked(user_id, bet, "CASINO_BET", f"Slots bet {bet}")
            if payout > 0:
                await self._credit_vc_unlocked(
                    user_id, payout, "CASINO_WIN", f"Slots {result_label}",
                )
            async with self._conn.execute(
                "SELECT vc_balance FROM users WHERE id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
            balance_after = int(row["vc_balance"])
            details = "|".join(symbols)
            await self._conn.execute(
                """
                INSERT INTO casino_games
                    (user_id, game_type, bet, result, payout, balance_before, balance_after,
                     details, created_at, interaction_id)
                VALUES (?, 'slots', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, bet, result_label, payout, balance_before, balance_after,
                    details, _utcnow(), interaction_id,
                ),
            )
            await self._conn.commit()
            return balance_before, balance_after
        except Exception:
            await self._conn.rollback()
            raise

    async def get_casino_history(self, user_id: int, limit: int = 10) -> list:
        from database.models import CasinoGameRecord

        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM casino_games WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [CasinoGameRecord.from_row(r) for r in rows]

    async def get_vc_stats(self, user_id: int) -> dict:
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) AS earned,
                COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 0) AS spent
            FROM coin_transactions WHERE user_id = ?
            """,
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        async with self._conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN payout > bet THEN payout - bet ELSE 0 END), 0) AS casino_profit,
                COALESCE(SUM(CASE WHEN payout < bet THEN bet - payout ELSE 0 END), 0) AS casino_loss,
                COUNT(*) AS casino_games
            FROM casino_games WHERE user_id = ?
            """,
            (user_id,),
        ) as cursor:
            c = await cursor.fetchone()
        return {
            "earned": int(row["earned"]),
            "spent": int(row["spent"]),
            "casino_profit": int(c["casino_profit"]),
            "casino_loss": int(c["casino_loss"]),
            "casino_games": int(c["casino_games"]),
        }
