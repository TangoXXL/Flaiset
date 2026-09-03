"""
Очередь матчмейкинга.

Игроки встают в очередь через /queue join. Когда набирается 10 человек
и на сервере нет активной игры, бот атомарно забирает первых 10
(claim_queue_match), балансирует команды по ELO (balance_teams),
назначает капитанов (максимальный ELO в каждой команде) и сразу
запускает PLAYING с голосовыми каналами — без ручного драфта.

Ручной режим /game + драфт капитанами остаётся отдельно.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from services.matchmaking import balance_teams
from utils.helpers import build_teams_ready_embed
from utils.permissions import is_admin

log = logging.getLogger("mlbb-bot.queue")


class Queue(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._match_lock = asyncio.Lock()

    queue_group = app_commands.Group(name="queue", description="Очередь на матч 5×5")

    @queue_group.command(name="join", description="Встать в очередь на матч")
    async def queue_join(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Команда только на сервере.", ephemeral=True)
            return

        db = self.bot.db
        user = await db.get_user_by_discord_id(interaction.user.id)
        if user is None:
            await interaction.response.send_message(
                "❌ Сначала зарегистрируйся через `/register`.", ephemeral=True
            )
            return

        await db.sync_username(interaction.user.id, interaction.user.display_name)

        if await db.is_player_in_active_game(interaction.guild_id, user.id):
            await interaction.response.send_message(
                "❌ Вы уже участвуете в активной игре.", ephemeral=True
            )
            return

        if config.guild_config(interaction.guild_id).required_voice_channel_id is not None:
            voice = interaction.user.voice if isinstance(interaction.user, discord.Member) else None
            in_channel = (
                voice is not None
                and voice.channel is not None
                and voice.channel.id == config.guild_config(interaction.guild_id).required_voice_channel_id
            )
            if not in_channel:
                await interaction.response.send_message(
                    "❌ Чтобы встать в очередь, зайдите в голосовой канал лобби.",
                    ephemeral=True,
                )
                return

        added, total, position = await db.join_queue(interaction.guild_id, user.id)
        if not added:
            if position <= 0:
                # BUG #13 fix: position 0 means join_queue's atomic guard
                # rejected us for being in an active game (raced with a
                # /game lobby join right after the earlier check above),
                # not because we were already queued.
                await interaction.response.send_message(
                    "❌ Вы уже участвуете в активной игре.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"⚠️ Вы уже в очереди (позиция {position}/{total}).", ephemeral=True
                )
            return

        await interaction.response.send_message(
            f"✅ Вы в очереди: **{position}/{total}** "
            f"(нужно {config.guild_config(interaction.guild_id).players_per_game} для старта).",
            ephemeral=True,
        )
        log.info(
            "Игрок %s встал в очередь guild=%s pos=%s/%s",
            user.discord_id, interaction.guild_id, position, total,
        )

        if total >= config.guild_config(interaction.guild_id).players_per_game:
            await self._try_start_match(interaction.guild, interaction.channel)

    @queue_group.command(name="leave", description="Выйти из очереди")
    async def queue_leave(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Команда только на сервере.", ephemeral=True)
            return

        db = self.bot.db
        user = await db.get_user_by_discord_id(interaction.user.id)
        if user is None:
            await interaction.response.send_message("❌ Вы не зарегистрированы.", ephemeral=True)
            return

        left = await db.leave_queue(interaction.guild_id, user.id)
        if not left:
            await interaction.response.send_message("⚠️ Вас не было в очереди.", ephemeral=True)
            return

        entries = await db.get_queue_entries(interaction.guild_id)
        await interaction.response.send_message(
            f"✅ Вы вышли из очереди. Сейчас в очереди: **{len(entries)}**.",
            ephemeral=True,
        )

    @queue_group.command(name="status", description="Показать текущую очередь")
    async def queue_status(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Команда только на сервере.", ephemeral=True)
            return

        entries = await self.bot.db.get_queue_entries(interaction.guild_id)
        embed = discord.Embed(
            title="📋 Очередь на матч",
            color=discord.Color.blurple(),
        )
        embed.description = (
            f"Игроков: **{len(entries)}/{config.guild_config(interaction.guild_id).players_per_game}**\n"
            f"Старт автоматический при наборе {config.guild_config(interaction.guild_id).players_per_game}."
        )
        if entries:
            listing = "\n".join(
                f"{i}. {e.player.discord_username} (ELO {e.player.elo})"
                for i, e in enumerate(entries, start=1)
            )
            embed.add_field(name="Список", value=listing, inline=False)
        else:
            embed.add_field(name="Список", value="*пусто*", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @queue_group.command(name="clear", description="Очистить очередь (админ)")
    @app_commands.default_permissions(manage_guild=True)
    async def queue_clear(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Команда только на сервере.", ephemeral=True)
            return
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ Только администратор.", ephemeral=True)
            return

        entries = await self.bot.db.get_queue_entries(interaction.guild_id)
        for entry in entries:
            await self.bot.db.leave_queue(interaction.guild_id, entry.player.id)

        await interaction.response.send_message(
            f"✅ Очередь очищена ({len(entries)} игроков).", ephemeral=True
        )

    async def _try_start_match(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable | None,
    ) -> None:
        """Пытается собрать матч из очереди. Под lock, чтобы два join'а
        не запустили два claim параллельно."""
        async with self._match_lock:
            db = self.bot.db
            try:
                game, players = await db.claim_queue_match(guild.id, config.guild_config(guild.id).players_per_game)
            except Exception:
                log.exception("claim_queue_match failed guild=%s", guild.id)
                return

            if game is None or len(players) < config.guild_config(guild.id).players_per_game:
                return

            log.info("Очередь: собрана игра #%s из %d игроков", game.game_id, len(players))

            # Fix (аудит, критический — п.2/п.11 ревью): всё, что происходит
            # МЕЖДУ claim_queue_match (игроки уже сняты с очереди, Game создан)
            # и моментом, когда игра становится "видимой и восстанавливаемой"
            # (объявление отправлено + lobby_message_id/draft_message_id
            # сохранены в БД), обёрнуто в один try/except. Раньше сбой на
            # любом из этих шагов (balance_teams, отправка embed в Discord,
            # отсутствие канала) либо не откатывал снятие игроков с очереди
            # вовсе (balance_teams: игра отменялась, но игроки не
            # возвращались — "терялись"), либо вовсе не отменял игру
            # (сбой отправки/канала: return без cancel — активная DRAFT-игра
            # без единого сообщения оставалась висеть навсегда и блокировала
            # /game и следующий claim_queue_match на этом сервере, так как
            # проверка "нет активной игры" не пропускала бы новую игру).
            # Теперь при любой ошибке на этом отрезке — единая атомарная
            # отмена + возврат игроков в очередь (cancel_and_requeue_atomic).
            try:
                blue, red = balance_teams(players)

                blue_cap = max(blue, key=lambda p: (p.elo, -p.id))
                red_cap = max(red, key=lambda p: (p.elo, -p.id))
                captains_set = await db.set_captains(game.game_id, blue_cap.id, red_cap.id)
                if not captains_set:
                    log.warning(
                        "Игра #%s: set_captains rejected after queue claim — cancel+requeue",
                        game.game_id,
                    )
                    await self._recover_failed_claim(game.game_id, guild.id, players)
                    return

                for p in blue:
                    if p.id != blue_cap.id:
                        await db.assign_team(game.game_id, p.id, "BLUE")
                for p in red:
                    if p.id != red_cap.id:
                        await db.assign_team(game.game_id, p.id, "RED")

                transitioned = await db.transition_game_status(game.game_id, "WAITING", "DRAFT")
                if not transitioned:
                    log.warning("Игра #%s: не удалось перевести WAITING→DRAFT после очереди", game.game_id)
                    await self._recover_failed_claim(game.game_id, guild.id, players)
                    return

                blue_team = [p for p in blue if p.id != blue_cap.id]
                red_team = [p for p in red if p.id != red_cap.id]

                announce = discord.Embed(
                    title=f"⚔️ Матч из очереди — Игра #{game.game_id}",
                    description=(
                        "Команды собраны автоматически по балансу ELO.\n"
                        f"👑 Синие — **{blue_cap.discord_username}**\n"
                        f"👑 Красные — **{red_cap.discord_username}**"
                    ),
                    color=discord.Color.green(),
                )
                announce.add_field(
                    name="🔵 Синяя",
                    value="\n".join(
                        f"{'👑' if p.id == blue_cap.id else '👤'} {p.discord_username} ({p.elo})"
                        for p in blue
                    ),
                    inline=True,
                )
                announce.add_field(
                    name="🔴 Красная",
                    value="\n".join(
                        f"{'👑' if p.id == red_cap.id else '👤'} {p.discord_username} ({p.elo})"
                        for p in red
                    ),
                    inline=True,
                )

                target = channel
                if target is None and config.guild_config(guild.id).results_channel_id:
                    target = self.bot.get_channel(config.guild_config(guild.id).results_channel_id)
                if target is None:
                    raise RuntimeError(f"Нет канала для объявления матча из очереди #{game.game_id}")

                message = await target.send(embed=announce)

                await db.set_lobby_message(game.game_id, message.channel.id, message.id)
                await db.set_draft_message(game.game_id, message.id)
            except Exception:
                log.exception(
                    "Не удалось довести матч #%s из очереди до объявления — отменяю игру и "
                    "возвращаю %d игроков в очередь",
                    game.game_id, len(players),
                )
                await self._recover_failed_claim(game.game_id, guild.id, players)
                return

            # С этого момента игра анонсирована и восстановима (lobby/draft
            # message id уже в БД) — сбой ниже НЕ должен приводить к отмене
            # матча вслепую: PLAYING/войсы могут уже быть реально созданы.
            # _finalize сам аккуратно обрабатывает частичные сбои создания
            # войсов (оставляет DRAFT + уведомление), поэтому здесь только
            # логируем неожиданное исключение и оставляем игру администратору
            # (доступен /cancel_game, а сообщение уже видно и восстановимо
            # после рестарта через recover_games()).
            try:
                # Переиспользуем DraftView._finalize для создания войсов и PLAYING.
                from cogs.game import DraftView

                draft_view = DraftView(self.bot, game.game_id, blue_cap, red_cap, available=[])
                draft_view.blue_team = blue_team
                draft_view.red_team = red_team
                draft_view.pick_index = DraftView.TOTAL_PICKS
                draft_view.message = message

                await draft_view._finalize()

                # Обновляем embed ссылками на войсы, если они создались.
                game = await db.get_game(game.game_id)
                if game is not None and game.status == "PLAYING":
                    blue_ch = (
                        guild.get_channel(game.blue_voice_channel_id)
                        if game.blue_voice_channel_id
                        else None
                    )
                    red_ch = (
                        guild.get_channel(game.red_voice_channel_id)
                        if game.red_voice_channel_id
                        else None
                    )
                    ready = build_teams_ready_embed(
                        game.game_id,
                        blue_cap,
                        blue_team,
                        red_cap,
                        red_team,
                        blue_channel=blue_ch if isinstance(blue_ch, discord.VoiceChannel) else None,
                        red_channel=red_ch if isinstance(red_ch, discord.VoiceChannel) else None,
                    )
                    try:
                        await message.edit(embed=ready)
                    except discord.HTTPException:
                        pass
            except Exception:
                log.exception(
                    "Игра #%s: ошибка при финализации матча из очереди уже ПОСЛЕ анонса — "
                    "игра остаётся видимой/восстановимой, требуется проверка администратором "
                    "(доступен /cancel_game)",
                    game.game_id,
                )

    async def _recover_failed_claim(self, game_id: int, guild_id: int, players: list) -> None:
        """Единая точка отмены + возврата в очередь при сбое на пути от
        claim_queue_match до анонса матча (см. комментарий в _try_start_match)."""
        try:
            await self.bot.db.cancel_and_requeue_atomic(
                game_id, guild_id, [p.id for p in players]
            )
        except Exception:
            log.exception(
                "Игра #%s: не удалось отменить и вернуть игроков в очередь после сбоя "
                "объявления — требуется ручная проверка администратором",
                game_id,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Queue(bot))
