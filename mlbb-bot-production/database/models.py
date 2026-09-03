"""
Дата-классы (модели) — тонкая типизированная обёртка над строками SQLite.

Это НЕ ORM: строки из sqlite/aiosqlite приходят как sqlite3.Row,
эти функции просто конвертируют их в удобные объекты с автодополнением
и понятными типами, чтобы в когах не было доступа по "магическим" строкам
вида row["elo"].
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class User:
    id: int
    discord_id: int
    discord_username: str
    mlbb_id: Optional[str]
    server_id: Optional[str]
    mlbb_nickname: Optional[str]
    verified: bool
    elo: int
    wins: int
    losses: int
    games_played: int
    created_at: str
    vc_balance: int = 0
    elo_protection: int = 0

    @property
    def win_rate(self) -> float:
        if self.games_played == 0:
            return 0.0
        return round(self.wins / self.games_played * 100, 1)

    @classmethod
    def from_row(cls, row) -> "User":
        keys = row.keys()
        return cls(
            id=row["id"],
            discord_id=row["discord_id"],
            discord_username=row["discord_username"],
            mlbb_id=row["mlbb_id"],
            server_id=row["server_id"],
            mlbb_nickname=row["mlbb_nickname"],
            verified=bool(row["verified"]),
            elo=row["elo"],
            wins=row["wins"],
            losses=row["losses"],
            games_played=row["games_played"],
            created_at=row["created_at"],
            vc_balance=int(row["vc_balance"]) if "vc_balance" in keys else 0,
            elo_protection=int(row["elo_protection"]) if "elo_protection" in keys else 0,
        )


@dataclass
class CoinTransaction:
    id: int
    user_id: int
    amount: int
    balance_after: int
    transaction_type: str
    description: Optional[str]
    game_id: Optional[int]
    created_at: str

    @classmethod
    def from_row(cls, row) -> "CoinTransaction":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            amount=row["amount"],
            balance_after=row["balance_after"],
            transaction_type=row["transaction_type"],
            description=row["description"],
            game_id=row["game_id"],
            created_at=row["created_at"],
        )


@dataclass
class CasinoGameRecord:
    id: int
    user_id: int
    game_type: str
    bet: int
    result: str
    payout: int
    balance_before: int
    balance_after: int
    details: Optional[str]
    created_at: str

    @classmethod
    def from_row(cls, row) -> "CasinoGameRecord":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            game_type=row["game_type"],
            bet=row["bet"],
            result=row["result"],
            payout=row["payout"],
            balance_before=row["balance_before"],
            balance_after=row["balance_after"],
            details=row["details"],
            created_at=row["created_at"],
        )


@dataclass
class Game:
    game_id: int
    guild_id: int
    status: str
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    blue_captain: Optional[int]
    red_captain: Optional[int]
    winner: Optional[str]
    screenshot_message_id: Optional[int]
    result_confirmed_by: Optional[int]
    result_confirmed_at: Optional[str]
    blue_voice_channel_id: Optional[int]
    red_voice_channel_id: Optional[int]
    lobby_channel_id: Optional[int] = None
    lobby_message_id: Optional[int] = None
    draft_message_id: Optional[int] = None
    draft_pick_deadline: Optional[str] = None
    confirm_notified_at: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "Game":
        return cls(**{key: row[key] for key in row.keys()})


@dataclass
class MatchRecord:
    """Одна строка истории матчей конкретного игрока (см. /matches, ТЗ п.34)."""

    game: Game
    team: Optional[str]
    captain: bool
    result: Optional[str]
    elo_change: Optional[int]
    blue_captain_name: Optional[str]
    red_captain_name: Optional[str]

    @classmethod
    def from_row(cls, row) -> "MatchRecord":
        game = Game(
            game_id=row["game_id"],
            guild_id=row["guild_id"],
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            blue_captain=row["blue_captain"],
            red_captain=row["red_captain"],
            winner=row["winner"],
            screenshot_message_id=row["screenshot_message_id"],
            result_confirmed_by=row["result_confirmed_by"],
            result_confirmed_at=row["result_confirmed_at"],
            blue_voice_channel_id=row["blue_voice_channel_id"],
            red_voice_channel_id=row["red_voice_channel_id"],
        )
        return cls(
            game=game,
            team=row["gp_team"],
            captain=bool(row["gp_captain"]),
            result=row["gp_result"],
            elo_change=row["gp_elo_change"],
            blue_captain_name=row["blue_captain_name"],
            red_captain_name=row["red_captain_name"],
        )


@dataclass
class QueueEntry:
    guild_id: int
    player: User
    queued_at: str

    @classmethod
    def from_row(cls, row) -> "QueueEntry":
        return cls(
            guild_id=row["queue_guild_id"],
            player=User.from_row(row),
            queued_at=row["queued_at"],
        )
