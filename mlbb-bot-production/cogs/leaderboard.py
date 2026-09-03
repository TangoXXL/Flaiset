"""
Ког лидерборда и истории матчей (Этапы 33-34 из ТЗ).

Ранговые бейджи/цвета — косметическая надстройка над ELO из utils/rank.py,
см. этот модуль: на сам ELO и логику начисления она не влияет.
"""

from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from utils.rank import get_rank, rank_progress

MEDALS = ["🥇", "🥈", "🥉"]


def _format_date(iso_str: str | None) -> str:
    if not iso_str:
        return "—"
    try:
        return datetime.fromisoformat(iso_str).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso_str


def _mini_bar(progress: float, length: int = 10) -> str:
    filled = max(0, min(length, round(progress * length)))
    return "▰" * filled + "▱" * (length - filled)


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="leaderboard", description="Топ игроков по ELO")
    async def leaderboard(self, interaction: discord.Interaction):
        top = await self.bot.db.get_leaderboard(limit=10)

        if not top:
            await interaction.response.send_message("Пока никто не зарегистрирован.", ephemeral=True)
            return

        lines = []
        for i, user in enumerate(top):
            prefix = MEDALS[i] if i < len(MEDALS) else f"`#{i + 1:>2}`"
            tier, next_tier, progress = rank_progress(user.elo)
            if next_tier is not None:
                progress_note = f"до {next_tier.name}: +{next_tier.min_elo - user.elo}"
            else:
                progress_note = "★ максимальный ранг"
            lines.append(
                f"{prefix} **{user.discord_username}** — **{user.elo}** ELO  ·  {tier.name}\n"
                f"┕ {_mini_bar(progress)}  {progress_note}"
            )

        top_tier = get_rank(top[0].elo)
        embed = discord.Embed(
            title="🏆 MLBB LEADERBOARD",
            description="\n\n".join(lines),
            color=discord.Color.from_rgb(*top_tier.color),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="matches", description="История матчей игрока")
    @app_commands.describe(member="Чью историю показать (по умолчанию — твою)")
    async def matches(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        user = await self.bot.db.get_user_by_discord_id(target.id)

        if user is None:
            text = (
                "❌ Ты ещё не зарегистрирован. Используй `/register`."
                if target.id == interaction.user.id
                else f"❌ У {target.mention} нет профиля."
            )
            await interaction.response.send_message(text, ephemeral=True)
            return

        matches = await self.bot.db.get_player_matches(user.id, limit=10)

        if not matches:
            await interaction.response.send_message(
                f"У {target.display_name} пока нет завершённых игр.", ephemeral=True
            )
            return

        tier = get_rank(user.elo)
        embed = discord.Embed(
            title=f"📜 История матчей — {target.display_name}",
            description=f"Текущий ранг: **{tier.name}** ({user.elo} ELO)",
            color=discord.Color.from_rgb(*tier.color),
        )

        for m in matches:
            won = m.result == "WIN"
            result_icon = "✅ Победа" if won else "❌ Поражение"
            sign = "+" if (m.elo_change or 0) >= 0 else ""
            team_label = "🔵 Синяя" if m.team == "BLUE" else "🔴 Красная"
            captain_label = " (капитан)" if m.captain else ""

            captains = f"🔵 {m.blue_captain_name or '—'} vs 🔴 {m.red_captain_name or '—'}"

            embed.add_field(
                name=f"Игра #{m.game.game_id} — {_format_date(m.game.finished_at)}",
                value=(
                    f"{team_label} команда{captain_label}\n"
                    f"Командиры: {captains}\n"
                    f"{result_icon} ({sign}{m.elo_change} ELO)"
                ),
                inline=False,
            )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Leaderboard(bot))
