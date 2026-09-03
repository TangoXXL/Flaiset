"""
Единая точка чтения конфигурации.

Все "магические" ID (каналы, категории, роли) должны браться отсюда,
а не быть захардкожены внутри когов — это то самое требование ТЗ
про "ID голосового канала должен храниться в конфигурации".

Ничего секретного здесь не хранится — секреты (токен) читаются из .env
через os.getenv и никогда не логируются и не коммитятся.
"""

import json
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _optional_snowflake(name: str) -> int | None:
    """Возвращает Discord ID из .env с понятной ошибкой при опечатке."""
    value = os.getenv(name)
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} должен содержать числовой Discord ID, получено: {value!r}") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} должен содержать положительный Discord ID")
    return parsed

# --- Обязательные переменные -------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN не найден. Проверь, что файл .env существует "
        "и содержит строку DISCORD_TOKEN=..."
    )

# --- Опциональные переменные (можно None на раннем этапе) -------------------
# ID гильдии (сервера) для разработки — slash-команды синкаются туда мгновенно.
DEV_GUILD_ID = _optional_snowflake("DEV_GUILD_ID")

# Канал, в котором игрок обязан находиться, чтобы нажать "Вступить" (Этап 9+).
REQUIRED_VOICE_CHANNEL_ID = _optional_snowflake("REQUIRED_VOICE_CHANNEL_ID")

# Категория, в которой создаются временные голосовые каналы команд (Этап 13+).
TEAM_VOICE_CATEGORY_ID = _optional_snowflake("TEAM_VOICE_CATEGORY_ID")

# Канал для приёма скриншотов результата (Этап 17+).
RESULTS_CHANNEL_ID = _optional_snowflake("RESULTS_CHANNEL_ID")

# ID роли администратора/модератора, которым разрешено подтверждать результат.
ADMIN_ROLE_ID = _optional_snowflake("ADMIN_ROLE_ID")

# --- Игровые константы --------------------------------------------------------
STARTING_ELO = 100
ELO_WIN = 25
ELO_LOSS = -25
MIN_ELO = 0
PLAYERS_PER_GAME = 10


@dataclass(frozen=True)
class GuildConfig:
    required_voice_channel_id: int | None = None
    team_voice_category_id: int | None = None
    results_channel_id: int | None = None
    admin_role_id: int | None = None
    players_per_game: int = PLAYERS_PER_GAME


def _parse_guild_configs() -> dict[int, GuildConfig]:
    """Parse optional per-guild overrides from GUILD_CONFIG_JSON.

    Example:
      GUILD_CONFIG_JSON={"123":{"results_channel_id":456,"admin_role_id":789}}

    Missing values inherit the legacy .env defaults, so upgrading remains
    backward compatible while allowing multiple Discord servers.
    """
    raw = os.getenv("GUILD_CONFIG_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GUILD_CONFIG_JSON содержит некорректный JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("GUILD_CONFIG_JSON должен быть JSON-объектом")

    defaults = GuildConfig(
        required_voice_channel_id=REQUIRED_VOICE_CHANNEL_ID,
        team_voice_category_id=TEAM_VOICE_CATEGORY_ID,
        results_channel_id=RESULTS_CHANNEL_ID,
        admin_role_id=ADMIN_ROLE_ID,
        players_per_game=PLAYERS_PER_GAME,
    )
    result: dict[int, GuildConfig] = {}
    for raw_guild_id, raw_cfg in data.items():
        try:
            guild_id = int(raw_guild_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Некорректный guild_id в GUILD_CONFIG_JSON: {raw_guild_id!r}") from exc
        if guild_id <= 0 or not isinstance(raw_cfg, dict):
            raise RuntimeError(f"Некорректная конфигурация guild {raw_guild_id!r}")
        values = {
            "required_voice_channel_id": defaults.required_voice_channel_id,
            "team_voice_category_id": defaults.team_voice_category_id,
            "results_channel_id": defaults.results_channel_id,
            "admin_role_id": defaults.admin_role_id,
            "players_per_game": defaults.players_per_game,
        }
        for key in values:
            if key not in raw_cfg:
                continue
            value = raw_cfg[key]
            if value is None and key != "players_per_game":
                values[key] = None
            else:
                try:
                    values[key] = int(value)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(f"guild {guild_id}: {key} должен быть числом") from exc
        if values["players_per_game"] < 2 or values["players_per_game"] % 2:
            raise RuntimeError(f"guild {guild_id}: players_per_game должен быть чётным и >= 2")
        result[guild_id] = GuildConfig(**values)
    return result


_GUILD_CONFIGS = _parse_guild_configs()
_DEFAULT_GUILD_CONFIG = GuildConfig(
    required_voice_channel_id=REQUIRED_VOICE_CHANNEL_ID,
    team_voice_category_id=TEAM_VOICE_CATEGORY_ID,
    results_channel_id=RESULTS_CHANNEL_ID,
    admin_role_id=ADMIN_ROLE_ID,
    players_per_game=PLAYERS_PER_GAME,
)

def guild_config(guild_id: int | None) -> GuildConfig:
    """Return per-guild config, falling back to legacy .env defaults."""
    if guild_id is None:
        return _DEFAULT_GUILD_CONFIG
    return _GUILD_CONFIGS.get(int(guild_id), _DEFAULT_GUILD_CONFIG)

# --- Valhalla Coin (VC) -------------------------------------------------------
WIN_COINS_MIN = 50
WIN_COINS_MAX = 80
LOSS_COINS_MIN = 1
LOSS_COINS_MAX = 50

# --- Shop ---------------------------------------------------------------------
ELO_PROTECTION_PRICE = 1000

# --- Casino -------------------------------------------------------------------
# Fix (аудит, п.8 — "соответствие текста UI реальной математике"): здесь
# раньше жили CASINO_SLOT_WEIGHTS / CASINO_SLOT_TRIPLE_MULT (per-symbol,
# 2×/3×/5×/8×/20×) / CASINO_SLOT_PAIR_MULT (0.5) — константы старой схемы
# выплат, которую services/casino.py больше не использует и никогда не
# импортирует (там своя фиксированная таблица _OUTCOMES с другим RTP).
# Текст команды /casino был скопирован именно с этих старых чисел (JACKPOT
# ×20, диапазон 2–8×, возврат 50%), поэтому годами показывал пользователям
# неверные множители. Оставлены только константы, которые реально
# используются кодом (CASINO_MIN_BET/MAX_BET/ALLOWED_BETS — валидация
# ставки; CASINO_RTP — сверяется тестом test_casino_configured_rtp_near_target
# с фактическим services.casino.CONFIGURED_RTP; CASINO_SLOT_SYMBOLS —
# алфавит символов для НЕ-джекпотных исходов). Если понадобится вернуть
# per-symbol множители — это отдельная фича, требующая согласованного
# изменения и в services/casino.py._OUTCOMES, и в тексте UI, и в тесте RTP.
CASINO_MIN_BET = 10
CASINO_MAX_BET = 1000
CASINO_ALLOWED_BETS = (10, 50, 100, 500, 1000)
CASINO_RTP = 0.95
CASINO_SLOT_SYMBOLS = ("🍒", "🍋", "🔔", "⭐", "💎")

# --- Пути ---------------------------------------------------------------------
DATABASE_PATH = os.getenv("DATABASE_PATH", "database.db")
HEALTH_HOST = os.getenv("HEALTH_HOST", "0.0.0.0")
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))

# --- Коги, загружаемые при старте ---------------------------------------------
INITIAL_EXTENSIONS = [
    "cogs.registration",
    "cogs.game",
    "cogs.results",
    "cogs.voice",
    "cogs.profile",
    "cogs.leaderboard",
    "cogs.admin",
    "cogs.queue",
    "cogs.shop",
    "cogs.casino",
]
