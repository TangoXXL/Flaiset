"""Казино: слоты за Valhalla Coin."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import discord
from discord import app_commands
from discord.ext import commands

import config
from services.casino import CONFIGURED_RTP, spin_slots

log = logging.getLogger("mlbb-bot.casino")


class BetSelect(discord.ui.Select):
    def __init__(self, cog: "Casino"):
        options = [
            discord.SelectOption(label=f"{b} VC", value=str(b), emoji="🪙")
            for b in config.CASINO_ALLOWED_BETS
        ]
        super().__init__(placeholder="Выберите ставку", min_values=1, max_values=1, options=options)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        bet = int(self.values[0])
        await self.cog._play_slots(interaction, bet)


class CasinoView(discord.ui.View):
    def __init__(self, cog: "Casino"):
        super().__init__(timeout=120)
        self.add_item(BetSelect(cog))


class Casino(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Per-user locks prevent double-spin on the same account. Refcounted
        # entries are removed when the last waiter/holder leaves so the dict
        # cannot grow without bound (BUG #18) without the race that plain
        # "delete if not locked()" prune had.
        self._locks: dict[int, tuple[asyncio.Lock, int]] = {}
        self._locks_guard = asyncio.Lock()

    @asynccontextmanager
    async def _user_lock(self, user_id: int) -> AsyncIterator[None]:
        """Acquire a per-user lock; drop the entry when the last user leaves."""
        async with self._locks_guard:
            entry = self._locks.get(user_id)
            if entry is None:
                entry = (asyncio.Lock(), 0)
                self._locks[user_id] = entry
            lock, refs = entry
            self._locks[user_id] = (lock, refs + 1)
        try:
            async with lock:
                yield
        finally:
            async with self._locks_guard:
                current = self._locks.get(user_id)
                if current is not None:
                    lock2, refs2 = current
                    if refs2 <= 1:
                        self._locks.pop(user_id, None)
                    else:
                        self._locks[user_id] = (lock2, refs2 - 1)

    @app_commands.command(name="casino", description="Казино Valhalla — слоты")
    async def casino(self, interaction: discord.Interaction):
        user = await self.bot.db.get_user_by_discord_id(interaction.user.id)
        if user is None:
            await interaction.response.send_message("❌ Сначала `/register`.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎰 VALHALLA CASINO",
            description=(
                f"💰 Баланс: **{user.vc_balance} VC**\n\n"
                "Выберите ставку и крутите слоты.\n"
                "💎💎💎 — JACKPOT ×10.67\n"
                "Тройка одинаковых символов — ×4\n"
                "Пара одинаковых символов — возврат ставки (без прибыли и без потерь)\n"
                f"Средняя отдача (RTP): ≈{CONFIGURED_RTP:.0%}"
            ),
            color=discord.Color.dark_purple(),
        )
        await interaction.response.send_message(
            embed=embed, view=CasinoView(self), ephemeral=True
        )

    @app_commands.command(name="casino_history", description="История игр в казино")
    async def casino_history(self, interaction: discord.Interaction):
        user = await self.bot.db.get_user_by_discord_id(interaction.user.id)
        if user is None:
            await interaction.response.send_message("❌ Сначала `/register`.", ephemeral=True)
            return
        rows = await self.bot.db.get_casino_history(user.id, limit=10)
        if not rows:
            await interaction.response.send_message("Пока нет игр.", ephemeral=True)
            return
        lines = []
        for r in rows:
            sign = "+" if r.payout > r.bet else ("0" if r.payout == r.bet else "")
            net = r.payout - r.bet
            lines.append(f"`{r.details or '—'}`  {r.result}  **{net:+d} VC**")
        embed = discord.Embed(
            title="🎰 Последние игры",
            description="\n".join(lines),
            color=discord.Color.dark_purple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _play_slots(self, interaction: discord.Interaction, bet: int):
        async with self._user_lock(interaction.user.id):
            db = self.bot.db
            user = await db.get_user_by_discord_id(interaction.user.id)
            if user is None:
                await interaction.response.send_message("❌ Сначала `/register`.", ephemeral=True)
                return
            if bet <= 0 or bet not in config.CASINO_ALLOWED_BETS:
                await interaction.response.send_message("❌ Недопустимая ставка.", ephemeral=True)
                return
            if user.vc_balance < bet:
                await interaction.response.send_message(
                    f"❌ Недостаточно VC (нужно {bet}, есть {user.vc_balance}).",
                    ephemeral=True,
                )
                return

            try:
                spin = spin_slots(bet)
                before, after = await db.play_casino_slots(
                    user.id,
                    bet,
                    spin.symbols,
                    spin.payout,
                    spin.result,
                    interaction_id=str(interaction.id),
                )
            except ValueError as exc:
                code = str(exc)
                if code == "insufficient_vc":
                    msg = "❌ Недостаточно VC."
                elif code == "duplicate_interaction":
                    msg = "⚠️ Эта ставка уже обработана."
                else:
                    msg = f"❌ {exc}"
                await interaction.response.send_message(msg, ephemeral=True)
                return

            reels = " ┃ ".join(spin.symbols)
            net = spin.payout - bet
            if spin.result == "JACKPOT":
                title = "💎 JACKPOT!"
                color = discord.Color.gold()
            elif spin.result == "TRIPLE":
                title = "🎉 Тройка!"
                color = discord.Color.green()
            elif spin.result == "PAIR":
                title = "✨ Пара"
                color = discord.Color.blue()
            else:
                title = "💀 Проигрыш"
                color = discord.Color.red()

            embed = discord.Embed(
                title=title,
                description=f"🎰 ┃ {reels} ┃",
                color=color,
            )
            embed.add_field(name="Ставка", value=f"{bet} VC", inline=True)
            embed.add_field(name="Выплата", value=f"{spin.payout} VC", inline=True)
            embed.add_field(name="Итог", value=f"**{net:+d} VC**", inline=True)
            embed.add_field(name="Баланс", value=f"{before} → **{after} VC**", inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            log.info(
                "Casino slots user=%s bet=%s result=%s payout=%s",
                interaction.user.id, bet, spin.result, spin.payout,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Casino(bot))
