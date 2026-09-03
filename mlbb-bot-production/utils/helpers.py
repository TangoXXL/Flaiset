"""
Общие вспомогательные функции для построения embed'ов.
"""

import discord

from config import PLAYERS_PER_GAME
from database.models import User


def build_lobby_embed(game_id: int, players: list[User], closed: bool = False) -> discord.Embed:
    """Embed набора игроков для /game (Этап 6 ТЗ)."""
    count = len(players)

    if closed:
        title = "🔒 НАБОР ЗАКРЫТ"
        color = discord.Color.gold()
    else:
        title = "🎮 MOBILE LEGENDS — НАБОР"
        color = discord.Color.blurple()

    embed = discord.Embed(title=title, color=color)
    embed.description = f"Собираем игроков на игру 5×5.\n\n👥 Участники: **{count}/{PLAYERS_PER_GAME}**"

    if players:
        listing = "\n".join(f"{i}. {p.discord_username}" for i, p in enumerate(players, start=1))
    else:
        listing = "*Пока никто не присоединился*"
    embed.add_field(name="Список игроков", value=listing, inline=False)

    embed.set_footer(text=f"Игра #{game_id}")
    return embed


def _team_lines(captain: User, members: list[User]) -> str:
    lines = [f"👑 {captain.discord_username}"]
    lines += [f"👤 {p.discord_username}" for p in members]
    return "\n".join(lines)


def build_draft_embed(
    game_id: int,
    blue_captain: User,
    blue_team: list[User],
    red_captain: User,
    red_team: list[User],
    available: list[User],
    current_captain: User,
    current_team: str,
) -> discord.Embed:
    """Embed интерфейса ручного драфта (ТЗ, п.14)."""
    embed = discord.Embed(title=f"🎮 DRAFT — ИГРА #{game_id}", color=discord.Color.orange())

    # blue_team / red_team здесь — участники КРОМЕ капитана
    embed.add_field(
        name="🔵 СИНЯЯ КОМАНДА",
        value=_team_lines(blue_captain, blue_team),
        inline=True,
    )
    embed.add_field(
        name="🔴 КРАСНАЯ КОМАНДА",
        value=_team_lines(red_captain, red_team),
        inline=True,
    )

    available_text = "\n".join(p.discord_username for p in available) or "*никого не осталось*"
    embed.add_field(name="👥 Доступные игроки", value=available_text, inline=False)

    team_emoji = "🔵" if current_team == "BLUE" else "🔴"
    embed.add_field(
        name="⏳ Сейчас выбирает",
        value=f"{team_emoji} {current_captain.discord_username}",
        inline=False,
    )
    return embed


def build_teams_ready_embed(
    game_id: int,
    blue_captain: User,
    blue_team: list[User],
    red_captain: User,
    red_team: list[User],
    blue_channel: discord.VoiceChannel | None = None,
    red_channel: discord.VoiceChannel | None = None,
) -> discord.Embed:
    """Embed после завершения драфта (ТЗ, п.16), опционально со ссылками
    на созданные голосовые каналы команд (ТЗ, п.17-19)."""
    embed = discord.Embed(title="🎮 КОМАНДЫ СОБРАНЫ", color=discord.Color.green())
    embed.add_field(name="🔵 СИНЯЯ КОМАНДА", value=_team_lines(blue_captain, blue_team), inline=True)
    embed.add_field(name="🔴 КРАСНАЯ КОМАНДА", value=_team_lines(red_captain, red_team), inline=True)
    embed.add_field(name="⚔️ 5 VS 5", value="🎮 Игра начинается!", inline=False)

    if blue_channel is not None and red_channel is not None:
        embed.add_field(
            name="🔊 Голосовые каналы",
            value=f"🔵 {blue_channel.mention}\n🔴 {red_channel.mention}",
            inline=False,
        )
    embed.set_footer(text=f"Игра #{game_id}")
    return embed


def _result_lines(entries: list[tuple[User, int, bool]]) -> str:
    """Format team lines for the result embed.

    The third tuple element is ``protection_used`` for *this match*
    (from ``game_players.protection_used``), not the player's current
    inventory of protection items.
    """
    lines = []
    for i, (user, change, protection_used) in enumerate(entries):
        icon = "👑" if i == 0 else "👤"
        sign = "+" if change >= 0 else ""
        prot = " 🛡️" if protection_used else ""
        lines.append(f"{icon} {user.discord_username} ({sign}{change} ELO){prot}")
    return "\n".join(lines) or "—"


def build_result_announcement_embed(
    game_id: int, winner: str, details: dict[str, list[tuple[User, int, bool]]]
) -> discord.Embed:
    """Публичное объявление результата (ТЗ, п.26) — показывается только
    ПОСЛЕ подтверждения администратором, с фактически применённым ELO
    (может отличаться от "сырых" +25/-25, если у кого-то ELO упёрся в 0)."""
    winner_label = "СИНЯЯ" if winner == "BLUE" else "КРАСНАЯ"
    color = discord.Color.blue() if winner == "BLUE" else discord.Color.red()

    embed = discord.Embed(title=f"🏆 {winner_label} КОМАНДА ПОБЕДИЛА!", color=color)
    embed.add_field(
        name="🔵 Синяя команда" + (" 🏆" if winner == "BLUE" else " ❌"),
        value=_result_lines(details["BLUE"]),
        inline=True,
    )
    embed.add_field(
        name="🔴 Красная команда" + (" 🏆" if winner == "RED" else " ❌"),
        value=_result_lines(details["RED"]),
        inline=True,
    )
    embed.set_footer(text=f"Игра #{game_id} завершена.")
    return embed
