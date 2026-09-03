"""Магазин Valhalla Coin."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("mlbb-bot.shop")


class ShopBuyView(discord.ui.View):
    def __init__(self, bot: commands.Bot, cog: "Shop"):
        super().__init__(timeout=120)
        self.bot = bot
        self.cog = cog

    @discord.ui.button(label="Купить Elo Protection", emoji="🛡️", style=discord.ButtonStyle.success)
    async def buy_protection(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Fix (аудит, п.4 — "shop double-click"): БД сама не даёт списать VC
        # в минус (purchase_elo_protection атомарна на уровне БД), но без
        # этой блокировки два быстрых клика по кнопке — два отдельных
        # Discord-interaction — оба спокойно проходят как две независимые,
        # последовательно обработанные покупки, если у игрока хватает VC на
        # обе. Это не "порча данных", но реальный шанс случайно купить
        # Elo Protection дважды одним двойным кликом. Лочим по пользователю
        # так же, как это уже сделано в cogs/casino.py._user_lock.
        async with self.cog._user_lock(interaction.user.id):
            db = self.bot.db
            user = await db.get_user_by_discord_id(interaction.user.id)
            if user is None:
                await interaction.response.send_message(
                    "❌ Сначала `/register`.", ephemeral=True
                )
                return

            if user.vc_balance < config.ELO_PROTECTION_PRICE:
                await interaction.response.send_message(
                    f"❌ Недостаточно VC. Нужно **{config.ELO_PROTECTION_PRICE}**, "
                    f"у вас **{user.vc_balance}**.",
                    ephemeral=True,
                )
                return

            try:
                balance, protection = await db.purchase_elo_protection(
                    user.id, config.ELO_PROTECTION_PRICE
                )
            except ValueError:
                await interaction.response.send_message(
                    "❌ Недостаточно VC (баланс изменился).", ephemeral=True
                )
                return

            embed = discord.Embed(
                title="✅ Покупка успешна!",
                description=(
                    f"Вы приобрели:\n\n🛡️ **Elo Protection** ×1\n\n"
                    f"💰 Потрачено: **{config.ELO_PROTECTION_PRICE} VC**\n"
                    f"💰 Остаток: **{balance} VC**\n"
                    f"🛡️ Защит: **{protection}**"
                ),
                color=discord.Color.green(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            log.info(
                "Shop: user %s bought Elo Protection, balance=%s prot=%s",
                interaction.user.id, balance, protection,
            )


class Shop(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Per-user locks with refcount so entries are removed when the last
        # waiter/holder leaves. Dict mutations are serialized by _locks_guard
        # to avoid races between create and cleanup (shop lock leak fix).
        self._locks: dict[int, tuple[asyncio.Lock, int]] = {}
        self._locks_guard = asyncio.Lock()

    @asynccontextmanager
    async def _user_lock(self, user_id: int) -> AsyncIterator[None]:
        """Acquire a per-user lock, then drop the entry when the last user leaves.

        Refcount tracks every coroutine that entered this context manager
        (holders + waiters), so cleanup cannot delete a lock another
        coroutine is about to use. Meta-lock only protects the dict; the
        per-user lock serializes the actual purchase.
        """
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

    @app_commands.command(name="shop", description="Магазин Valhalla Coin")
    async def shop(self, interaction: discord.Interaction):
        user = await self.bot.db.get_user_by_discord_id(interaction.user.id)
        balance = user.vc_balance if user else 0
        protection = user.elo_protection if user else 0

        embed = discord.Embed(
            title="🛒 VALHALLA SHOP",
            description=f"💰 Ваш баланс: **{balance} VC**\n🛡️ Elo Protection: **{protection}**",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="🛡️ Elo Protection",
            value=(
                f"Цена: **{config.ELO_PROTECTION_PRICE} VC**\n"
                "Защищает от потери Elo при **следующем поражении**.\n"
                "При победе защита не расходуется."
            ),
            inline=False,
        )
        await interaction.response.send_message(
            embed=embed, view=ShopBuyView(self.bot, self), ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Shop(bot))
