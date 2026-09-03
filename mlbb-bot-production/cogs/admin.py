"""
Административный ког.

/cancel_game (Этап 41): отмена активной игры, ELO не начисляется.

/edit_result (Этап 31): исправление уже подтверждённого результата.
Полный откат + повторное применение, а не "разностное" исправление —
так проще гарантировать, что elo_history и users.elo не разойдутся:
  1. revert_game_result — отменяет именно то, что было реально начислено
     (с учётом clamp на 0), а не "сырые" +25/-25;
  2. apply_game_result — начисляет ELO по НОВОМУ победителю с нуля;
  3. update_game_winner — обновляет games.winner и данные подтверждения.
Оба шага пишут отдельные записи в elo_history, так что по истории видно
и откат, и исправление — ничего не подчищается задним числом.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.helpers import build_result_announcement_embed
from utils.permissions import is_admin

log = logging.getLogger("mlbb-bot.admin")


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="cancel_game", description="Отменить текущую активную игру на сервере")
    @app_commands.default_permissions(manage_guild=True)
    async def cancel_game(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Эта команда работает только на сервере.", ephemeral=True)
            return

        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ Только администратор может отменить игру.", ephemeral=True)
            return

        db = self.bot.db
        game = await db.get_active_game(interaction.guild_id)
        if game is None:
            await interaction.response.send_message("❌ Нет активной игры для отмены.", ephemeral=True)
            return

        # Fix (round 3, критический): раньше статус переписывался на
        # CANCELLED безусловным UPDATE, без проверки, что игра всё ещё
        # активна на момент записи. Если между чтением game (строкой выше)
        # и этим UPDATE игра успевала завершиться обычным путём — например,
        # администратор подтвердил результат через DM-кнопку буквально в ту
        # же секунду, — ELO уже был бы начислен всем игрокам, а эта команда
        # молча перезаписала бы games.status на CANCELLED поверх COMPLETED,
        # оставив состояние противоречивым (ELO начислен, но игра "отменена").
        # cancel_active_game_atomic — атомарный CAS: применяется, только
        # если статус всё ещё НЕ COMPLETED/CANCELLED к моменту выполнения
        # UPDATE в БД.
        cancelled = await db.cancel_active_game_atomic(game.game_id)
        if not cancelled:
            await interaction.response.send_message(
                "❌ Не удалось отменить: игра уже завершилась (результат подтверждён или уже отменена) "
                "буквально перед этим. Проверьте текущий статус.",
                ephemeral=True,
            )
            return

        deleted_channels = []
        # Fix (round 2, #4) + Fix (round 3, критический — требование п.4
        # ревью): каждый канал теперь обрабатывается и очищается в БД
        # независимо. Раньше один общий флаг had_channel_ids приводил к
        # тому, что clear_voice_channels() стирал ОБА ID разом, даже если
        # реально обработан (удалён/подтверждённо отсутствует) был только
        # один из двух каналов — второй, у которого удаление действительно
        # упало с ошибкой, "терялся" в БД и оставался в гильдии навсегда.
        resolved = {"blue": False, "red": False}
        for label, channel_id in (
            ("blue", game.blue_voice_channel_id),
            ("red", game.red_voice_channel_id),
        ):
            if channel_id is None:
                resolved[label] = True
                continue
            channel = interaction.guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.NotFound:
                    resolved[label] = True  # канала уже нет — считаем его отменённым/удалённым
                    continue
                except discord.HTTPException:
                    log.exception(
                        "Не удалось проверить голосовой канал %s при отмене игры #%s",
                        channel_id, game.game_id,
                    )
                    continue
            try:
                await channel.delete(reason=f"Игра #{game.game_id} отменена администратором")
                deleted_channels.append(channel.name)
                resolved[label] = True
            except discord.HTTPException:
                log.exception("Не удалось удалить голосовой канал %s при отмене игры #%s", channel_id, game.game_id)
                resolved[label] = False

        if resolved["blue"] or resolved["red"]:
            # Fix #7: обнуляем в БД только те ID каналов, что реально
            # обработаны — если один канал не удалось удалить, его ссылка
            # остаётся в БД, и cleanup_stale_voice_channels подхватит его
            # при следующем рестарте бота.
            await db.clear_voice_channels(
                game.game_id, clear_blue=resolved["blue"], clear_red=resolved["red"]
            )

        embed = discord.Embed(
            title="🚫 Игра отменена",
            description=(
                f"Игра #{game.game_id} отменена администратором {interaction.user.mention}.\n"
                "ELO не изменялся, результат не засчитан."
            ),
            color=discord.Color.dark_grey(),
        )
        await interaction.response.send_message(embed=embed)
        log.info(
            "Игра #%s отменена администратором %s (удалено каналов: %d)",
            game.game_id, interaction.user.id, len(deleted_channels),
        )

    @app_commands.command(name="edit_result", description="Исправить результат уже подтверждённой игры")
    @app_commands.describe(game_id="ID игры (см. подвал сообщения с результатом)", winner="Правильный победитель")
    @app_commands.choices(
        winner=[
            app_commands.Choice(name="Синяя команда", value="BLUE"),
            app_commands.Choice(name="Красная команда", value="RED"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    async def edit_result(
        self, interaction: discord.Interaction, game_id: int, winner: app_commands.Choice[str]
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Эта команда работает только на сервере.", ephemeral=True)
            return

        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Только администратор может исправлять результат.", ephemeral=True
            )
            return

        db = self.bot.db
        game = await db.get_game(game_id)

        if game is None or game.guild_id != interaction.guild_id:
            await interaction.response.send_message(f"❌ Игра #{game_id} не найдена на этом сервере.", ephemeral=True)
            return

        if game.status != "COMPLETED":
            await interaction.response.send_message(
                f"❌ Исправлять можно только уже подтверждённые игры (текущий статус: {game.status}).",
                ephemeral=True,
            )
            return

        if game.winner == winner.value:
            await interaction.response.send_message(
                "⚠️ Победитель уже указан именно так — исправление не требуется.", ephemeral=True
            )
            return

        # BUG #15: refuse edit when any participant already has a later
        # COMPLETED match. Reverting only this game's ELO without replaying
        # every subsequent elo_history row would leave intermediate ratings
        # wrong. Full replay is out of scope; blocking is the safe choice.
        if await db.has_later_completed_game(game_id):
            await interaction.response.send_message(
                "❌ Нельзя исправить эту игру: у одного или нескольких участников "
                "уже есть более поздние завершённые матчи. Исправление сломало бы "
                "промежуточный ELO. Отмените/пересчитайте более новые матчи сначала "
                "или правьте только самую свежую игру.",
                ephemeral=True,
            )
            return

        # BUG #7 fix: edit_game_result_atomic reverts and re-applies VC/ELO
        # for up to 10 players in one transaction — comfortably slower than
        # Discord's 3-second ack window under load. Defer now so the DB
        # work below can never cause a false "interaction failed" once it
        # has already committed.
        await interaction.response.defer(ephemeral=True)

        old_winner = game.winner

        # Fix #2 (та же критическая проблема, что и в results.py): откат
        # старого начисления + начисление по новому победителю + запись
        # нового winner теперь одна атомарная транзакция (edit_game_result_atomic).
        # Раньше это были раздельные commit'ы — при сбое посередине можно было
        # получить состояние "ELO уже откачен, а новый ещё не начислен".
        blue_team = await db.get_team_players(game_id, "BLUE")
        red_team = await db.get_team_players(game_id, "RED")
        winners = blue_team if winner.value == "BLUE" else red_team
        losers = red_team if winner.value == "BLUE" else blue_team

        # Fix (round 3, критический): CAS-защита от гонки двух одновременных
        # /edit_result для одной и той же игры — expected_current_winner
        # должен всё ещё совпадать с тем, что мы только что прочитали выше
        # (old_winner), иначе кто-то другой уже изменил результат в этот
        # самый момент, и применять наш "устаревший" revert+apply опасно.
        from services.economy import roll_match_vc

        win_vc_map = {p.id: roll_match_vc(True) for p in winners}
        loss_vc_map = {p.id: roll_match_vc(False) for p in losers}

        edited = await db.edit_game_result_atomic(
            game_id,
            expected_current_winner=old_winner,
            new_winner=winner.value,
            confirmed_by=interaction.user.id,
            new_winner_ids=[p.id for p in winners],
            new_loser_ids=[p.id for p in losers],
            elo_win=config.ELO_WIN,
            elo_loss=config.ELO_LOSS,
            min_elo=config.MIN_ELO,
            win_vc_map=win_vc_map,
            loss_vc_map=loss_vc_map,
        )
        if not edited:
            await interaction.followup.send(
                "❌ Не удалось исправить результат: похоже, кто-то ещё изменил результат этой игры "
                "буквально одновременно с вами. Проверьте текущий результат и попробуйте снова.",
                ephemeral=True,
            )
            return

        details = await db.get_game_result_details(game_id)
        embed = build_result_announcement_embed(game_id, winner.value, details)
        embed.title = "✏️ РЕЗУЛЬТАТ ИСПРАВЛЕН — " + embed.title

        old_label = "синяя" if old_winner == "BLUE" else "красная" if old_winner == "RED" else "не определена"
        new_label = "синяя" if winner.value == "BLUE" else "красная"

        channel = self.bot.get_channel(config.guild_config(interaction.guild_id).results_channel_id) if config.guild_config(interaction.guild_id).results_channel_id else None
        if channel is not None:
            await channel.send(
                content=(
                    f"⚠️ Администратор {interaction.user.mention} исправил результат игры #{game_id}: "
                    f"было — {old_label}, стало — **{new_label}** команда."
                ),
                embed=embed,
            )

        await interaction.followup.send(
            f"✅ Результат игры #{game_id} исправлен. Новый победитель: {new_label} команда. "
            f"ELO пересчитан у всех участников.",
            ephemeral=True,
        )
        log.info(
            "Игра #%s: результат исправлен администратором %s (%s -> %s)",
            game_id, interaction.user.id, old_winner, winner.value,
        )

    @app_commands.command(
        name="confirm_result",
        description="Резервное подтверждение результата (если у всех админов закрыты DM)",
    )
    @app_commands.describe(game_id="ID игры, ожидающей подтверждения", winner="Победитель")
    @app_commands.choices(
        winner=[
            app_commands.Choice(name="Синяя команда", value="BLUE"),
            app_commands.Choice(name="Красная команда", value="RED"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    async def confirm_result(
        self, interaction: discord.Interaction, game_id: int, winner: app_commands.Choice[str]
    ):
        """Fix (round 2, #10): резервный путь подтверждения результата.
        Обычный сценарий — кнопки в DM (cogs/results.py). Но если у ВСЕХ
        администраторов закрыты личные сообщения, кнопки никому не
        приходят, и результат мог бы "зависнуть" в PENDING_CONFIRMATION
        навсегда (даже после рестарта — recover_pending_confirmations
        столкнётся с той же проблемой). Эта slash-команда не зависит от DM
        и использует ровно ту же атомарную логику (finalize_result), что
        и кнопки, включая защиту от двойного подтверждения и проверку
        состава команд."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Эта команда работает только на сервере.", ephemeral=True)
            return

        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Только администратор может подтверждать результат.", ephemeral=True
            )
            return

        db = self.bot.db
        game = await db.get_game(game_id)
        if game is None or game.guild_id != interaction.guild_id:
            await interaction.response.send_message(f"❌ Игра #{game_id} не найдена на этом сервере.", ephemeral=True)
            return

        if game.status != "PENDING_CONFIRMATION":
            await interaction.response.send_message(
                f"❌ Эту игру сейчас нельзя подтвердить (статус: {game.status}).", ephemeral=True
            )
            return

        results_cog = self.bot.get_cog("Results")
        if results_cog is None:
            await interaction.response.send_message("❌ Внутренняя ошибка: ког результатов не загружен.", ephemeral=True)
            return

        # BUG #7 fix: same reasoning as /edit_result — finalize_result does
        # a full transaction plus public announcement, defer immediately.
        await interaction.response.defer(ephemeral=True)

        success = await results_cog.confirm_result_command(game_id, winner.value, confirmed_by=interaction.user.id)
        if success:
            team_label = "синяя" if winner.value == "BLUE" else "красная"
            await interaction.followup.send(
                f"✅ Принято: победила {team_label} команда. ELO начислен, результат объявлен.", ephemeral=True
            )
            log.info("Игра #%s: подтверждена через /confirm_result администратором %s", game_id, interaction.user.id)
        else:
            await interaction.followup.send(
                "❌ Не удалось подтвердить: результат уже подтверждён другим администратором, "
                "либо состав команд повреждён (см. логи бота).",
                ephemeral=True,
            )


    admin_elo = app_commands.Group(name="admin_elo", description="Админ: управление ELO")
    admin_coins = app_commands.Group(name="admin_coins", description="Админ: управление Valhalla Coin")

    @admin_elo.command(name="set", description="Установить ELO игроку")
    @app_commands.describe(member="Игрок", value="Новое значение ELO (>= 0)")
    @app_commands.default_permissions(manage_guild=True)
    async def elo_set(self, interaction: discord.Interaction, member: discord.Member, value: int):
        if not await self._guard_admin(interaction):
            return
        if value < 0:
            await interaction.response.send_message("❌ ELO не может быть отрицательным.", ephemeral=True)
            return
        user = await self.bot.db.get_user_by_discord_id(member.id)
        if user is None:
            await interaction.response.send_message("❌ Игрок не зарегистрирован.", ephemeral=True)
            return
        old, new = await self.bot.db.admin_set_elo(user.id, value, interaction.user.id, "set")
        from utils.rank import get_rank
        await interaction.response.send_message(
            f"✅ ELO {member.mention}: **{old}** → **{new}** ({get_rank(new).name})",
            ephemeral=True,
        )

    @admin_elo.command(name="add", description="Добавить ELO игроку")
    @app_commands.describe(member="Игрок", amount="Сколько добавить")
    @app_commands.default_permissions(manage_guild=True)
    async def elo_add(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if not await self._guard_admin(interaction):
            return
        if amount <= 0:
            await interaction.response.send_message("❌ amount должен быть > 0. Для снятия используйте `/admin_elo remove`.", ephemeral=True)
            return
        user = await self.bot.db.get_user_by_discord_id(member.id)
        if user is None:
            await interaction.response.send_message("❌ Игрок не зарегистрирован.", ephemeral=True)
            return
        # BUG #12 fix: atomic read-modify-write instead of computing
        # new_val from a value read outside the write lock.
        old, new = await self.bot.db.admin_adjust_elo(user.id, amount, interaction.user.id, f"add:{amount}")
        from utils.rank import get_rank
        await interaction.response.send_message(
            f"✅ ELO {member.mention}: **{old}** → **{new}** ({get_rank(new).name})",
            ephemeral=True,
        )

    @admin_elo.command(name="remove", description="Снять ELO у игрока")
    @app_commands.describe(member="Игрок", amount="Сколько снять")
    @app_commands.default_permissions(manage_guild=True)
    async def elo_remove(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if not await self._guard_admin(interaction):
            return
        if amount <= 0:
            await interaction.response.send_message("❌ amount должен быть > 0.", ephemeral=True)
            return
        user = await self.bot.db.get_user_by_discord_id(member.id)
        if user is None:
            await interaction.response.send_message("❌ Игрок не зарегистрирован.", ephemeral=True)
            return
        # BUG #12 fix: atomic read-modify-write (clamped to >= 0 inside the lock).
        old, new = await self.bot.db.admin_adjust_elo(user.id, -amount, interaction.user.id, f"remove:{amount}")
        from utils.rank import get_rank
        await interaction.response.send_message(
            f"✅ ELO {member.mention}: **{old}** → **{new}** ({get_rank(new).name})",
            ephemeral=True,
        )

    @admin_coins.command(name="add", description="Начислить VC")
    @app_commands.describe(member="Игрок", amount="Сумма VC")
    @app_commands.default_permissions(manage_guild=True)
    async def coins_add(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if not await self._guard_admin(interaction):
            return
        if amount <= 0:
            await interaction.response.send_message("❌ amount > 0.", ephemeral=True)
            return
        user = await self.bot.db.get_user_by_discord_id(member.id)
        if user is None:
            await interaction.response.send_message("❌ Игрок не зарегистрирован.", ephemeral=True)
            return
        bal = await self.bot.db.credit_vc(user.id, amount, "ADMIN_GRANT", f"Admin {interaction.user.id}")
        await interaction.response.send_message(
            f"✅ +{amount} VC → {member.mention}. Баланс: **{bal} VC**", ephemeral=True
        )

    @admin_coins.command(name="remove", description="Списать VC")
    @app_commands.describe(member="Игрок", amount="Сумма VC")
    @app_commands.default_permissions(manage_guild=True)
    async def coins_remove(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if not await self._guard_admin(interaction):
            return
        if amount <= 0:
            await interaction.response.send_message("❌ amount > 0.", ephemeral=True)
            return
        user = await self.bot.db.get_user_by_discord_id(member.id)
        if user is None:
            await interaction.response.send_message("❌ Игрок не зарегистрирован.", ephemeral=True)
            return
        try:
            bal = await self.bot.db.debit_vc(user.id, amount, "ADMIN_REMOVE", f"Admin {interaction.user.id}")
        except ValueError:
            await interaction.response.send_message("❌ Недостаточно VC у игрока.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ −{amount} VC → {member.mention}. Баланс: **{bal} VC**", ephemeral=True
        )

    @admin_coins.command(name="set", description="Установить баланс VC")
    @app_commands.describe(member="Игрок", value="Новый баланс (>= 0)")
    @app_commands.default_permissions(manage_guild=True)
    async def coins_set(self, interaction: discord.Interaction, member: discord.Member, value: int):
        if not await self._guard_admin(interaction):
            return
        if value < 0:
            await interaction.response.send_message("❌ Баланс не может быть < 0.", ephemeral=True)
            return
        user = await self.bot.db.get_user_by_discord_id(member.id)
        if user is None:
            await interaction.response.send_message("❌ Игрок не зарегистрирован.", ephemeral=True)
            return
        bal = await self.bot.db.set_vc_balance(user.id, value, f"Admin set by {interaction.user.id}")
        await interaction.response.send_message(
            f"✅ Баланс {member.mention}: **{bal} VC**", ephemeral=True
        )

    @admin_coins.command(name="history", description="История VC игрока")
    @app_commands.describe(member="Игрок")
    @app_commands.default_permissions(manage_guild=True)
    async def coins_history(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._guard_admin(interaction):
            return
        user = await self.bot.db.get_user_by_discord_id(member.id)
        if user is None:
            await interaction.response.send_message("❌ Игрок не зарегистрирован.", ephemeral=True)
            return
        rows = await self.bot.db.get_coin_transactions(user.id, limit=15)
        if not rows:
            await interaction.response.send_message("История пуста.", ephemeral=True)
            return
        lines = [
            f"`{r.created_at[:16]}` **{r.amount:+d}** ({r.transaction_type}) → {r.balance_after}"
            for r in rows
        ]
        embed = discord.Embed(
            title=f"💰 VC history — {member.display_name}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _guard_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Только на сервере.", ephemeral=True)
            return False
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return False
        return True


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
