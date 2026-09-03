"""
Ког профиля игрока.

/profile без аргумента показывает профиль автора команды.
С аргументом member — профиль другого участника сервера
(полезно для админов/любопытных).

Актуальное ТЗ, п.2-3/п.19: MLBB Nickname и "Статус аккаунта" ("🟡 Не
подтверждён") в интерфейсе профиля не показываются — верификации сейчас
нет и не будет. Профиль отображает только: Discord, MLBB Player ID,
Server ID, ELO и игровую статистику.

Визуал: профиль рендерится как PNG-карточка (utils/profile_card.py) с
фоном/рамкой в цвете рангового тира (utils/rank.py — косметическая
надстройка над ELO, см. модуль). Если рендер по какой-то причине не
удался (например, не удалось скачать аватар, либо сломались шрифты на
хостинге) — код откатывается на обычный текстовый embed, чтобы /profile
не переставал работать целиком из-за проблемы с картинкой.
"""

import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.profile_card import build_profile_card_png
from utils.rank import get_rank

log = logging.getLogger("mlbb-bot.profile")


class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="profile", description="Показать профиль игрока Mobile Legends")
    @app_commands.describe(member="Чей профиль показать (по умолчанию — твой)")
    async def profile(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        user = await self.bot.db.get_user_by_discord_id(target.id)

        if user is None:
            if target.id == interaction.user.id:
                text = "❌ Ты ещё не зарегистрирован. Используй `/register`, чтобы создать профиль."
            else:
                text = f"❌ У {target.mention} нет профиля — он ещё не использовал `/register`."
            await interaction.response.send_message(text, ephemeral=True)
            return

        # Discord username мог измениться с момента регистрации — освежаем его.
        if user.discord_username != str(target):
            await self.bot.db.sync_username(target.id, str(target))

        tier = get_rank(user.elo)

        # Рендер карточки (сеть за аватаром + Pillow) может занять больше
        # 3 секунд, за которые Discord ждёт первый ответ — поэтому сначала
        # defer(), а сам результат отправляем через followup.
        await interaction.response.defer()

        try:
            avatar_bytes = await target.display_avatar.replace(size=256, format="png").read()
            png_bytes = await build_profile_card_png(
                display_name=target.display_name,
                player_id=user.mlbb_id or "—",
                server_id=user.server_id or "—",
                elo=user.elo,
                wins=user.wins,
                losses=user.losses,
                games=user.games_played,
                win_rate=user.win_rate,
                avatar_bytes=avatar_bytes,
            )
            file = discord.File(io.BytesIO(png_bytes), filename="profile_card.png")
            embed = discord.Embed(color=discord.Color.from_rgb(*tier.color))
            embed.set_image(url="attachment://profile_card.png")
            await interaction.followup.send(embed=embed, file=file)
        except Exception:
            log.exception("Не удалось отрендерить карточку профиля — откатываюсь на текстовый embed")
            embed = discord.Embed(
                title=f"📇 Профиль — {target.display_name}",
                color=discord.Color.from_rgb(*tier.color),
            )
            embed.set_thumbnail(url=target.display_avatar.url)
            embed.add_field(name="👤 Discord", value=target.mention, inline=True)
            embed.add_field(name="🎮 MLBB Player ID", value=user.mlbb_id or "—", inline=True)
            embed.add_field(name="🌐 Server ID", value=user.server_id or "—", inline=True)
            embed.add_field(name="⭐ ELO", value=f"{user.elo} ({tier.name})", inline=True)
            embed.add_field(name="💰 Valhalla Coin", value=f"{user.vc_balance} VC", inline=True)
            embed.add_field(name="🛡️ Elo Protection", value=str(user.elo_protection), inline=True)
            embed.add_field(name="🏆 Побед", value=str(user.wins), inline=True)
            embed.add_field(name="💀 Поражений", value=str(user.losses), inline=True)
            embed.add_field(name="🎮 Игр", value=str(user.games_played), inline=True)
            embed.add_field(name="📊 Win Rate", value=f"{user.win_rate}%", inline=True)
            try:
                stats = await self.bot.db.get_vc_stats(user.id)
                embed.add_field(
                    name="📈 Экономика",
                    value=(
                        f"VC earned: {stats['earned']}\n"
                        f"VC spent: {stats['spent']}\n"
                        f"Casino games: {stats['casino_games']}"
                    ),
                    inline=False,
                )
            except Exception:
                pass
            await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Profile(bot))
