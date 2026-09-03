"""
Ког результатов игры (Этапы 23-30 и 42 из ТЗ).

Реализовано:
  - Приём скриншота результата в RESULTS_CHANNEL_ID, только от участников
    активной игры в статусе PLAYING (Этап 23).
  - Скрытый интерфейс подтверждения для администратора: бот НЕ постит
    кнопки в общий канал (там их увидели бы игроки), а отправляет их
    личным сообщением (DM) каждому, кто проходит is_admin(). Это и есть
    "обычные игроки не должны видеть этот интерфейс" из ТЗ, п.24 —
    выполнено через приватный канал доставки, а не через ephemeral
    (ephemeral здесь неприменим: скриншот прилетает обычным сообщением,
    а не через slash-команду, так что нет интеракции, на которую можно
    ответить ephemeral).
  - На каждый клик — повторная проверка прав (п.24, "нельзя полагаться
    только на то, что кнопка визуально скрыта").
  - Атомарное подтверждение победителя через try_confirm_winner —
    защита от повторного начисления ELO, даже если два администратора
    нажмут кнопку почти одновременно (Этап 30).
  - Начисление +25/-25 ELO с защитой от ухода в минус (Этап 27-29).
  - Публичное объявление результата ПОСЛЕ подтверждения (Этап 26).
  - Удаление временных голосовых каналов через 15 секунд после
    завершения игры (Этап 42).
  - Восстановление PENDING_CONFIRMATION после рестарта бота: старые DM
    с кнопками теряются (это не persistent view, у каждого админа своё
    сообщение, отслеживать все — усложнение не по этому этапу), поэтому
    при старте бот просто отправляет админам новый запрос подтверждения
    для каждой такой игры — сам результат/статус при этом не теряется,
    он всё это время хранился в БД (Этап 39).

/edit_result (исправление уже подтверждённого результата, Этап 31)
реализован отдельно в cogs/admin.py — он переиспользует apply_game_result
отсюда же, но саму команду и revert-логику держим в admin.py, так как
это административное действие, а не часть обычного игрового цикла.
"""

import asyncio
import logging

import discord
from discord.ext import commands

import config
from utils.helpers import build_result_announcement_embed
from utils.permissions import is_admin

log = logging.getLogger("mlbb-bot.results")

VOICE_CHANNEL_CLEANUP_DELAY = 15  # секунд, см. ТЗ п.42 (10-30 сек)


class ConfirmResultView(discord.ui.View):
    """Отправляется ЛИЧНЫМ сообщением каждому администратору — обычные
    игроки этот View никогда не видят."""

    def __init__(self, bot: commands.Bot, game_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.game_id = game_id

    async def _guarded_resolve(self, interaction: discord.Interaction, winner: str):
        # Fix #1 (критическая ошибка): в личных сообщениях (DM) Discord отдаёт
        # interaction.user как discord.User, а НЕ discord.Member — у discord.User
        # нет ролей вообще, поэтому старая проверка "isinstance(..., discord.Member)"
        # всегда была False в DM, и ни один администратор не мог подтвердить
        # результат. Чтобы понять, администратор ли нажавший, нужно явно найти
        # его как Member на сервере, где идёт игра (game.guild_id), и уже
        # у этого Member проверять роль/права.
        game = await self.bot.db.get_game(self.game_id)
        if game is None:
            await interaction.response.send_message("❌ Игра не найдена.", ephemeral=True)
            return

        guild = self.bot.get_guild(game.guild_id)
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(game.guild_id)
            except discord.HTTPException:
                guild = None

        member: discord.Member | None = None
        if guild is not None:
            member = guild.get_member(interaction.user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(interaction.user.id)
                except discord.HTTPException:
                    member = None

        if member is None or not is_admin(member):
            await interaction.response.send_message(
                "❌ У вас нет прав подтверждать результат этой игры.", ephemeral=True
            )
            return

        # BUG #7 fix: finalize_result does a whole transaction plus editing
        # every other admin's DM message, which can easily exceed Discord's
        # 3-second interaction ack window. Deferring immediately means the
        # interaction is acknowledged right away regardless of how long the
        # rest takes, and the actual outcome always goes out through
        # followup — no more "interaction failed" while the DB has already
        # committed the result.
        await interaction.response.defer(ephemeral=True)

        cog: Results = self.bot.get_cog("Results")  # type: ignore[assignment]
        success = await cog.finalize_result(self.game_id, winner, confirmed_by=member.id)

        if success:
            team_label = "синяя" if winner == "BLUE" else "красная"
            await interaction.followup.send(
                f"✅ Принято: победила {team_label} команда. ELO начислен, результат объявлен.",
                ephemeral=True,
            )
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.followup.send(
                "❌ Не удалось подтвердить результат: либо он уже подтверждён, либо состав команд "
                "повреждён (проверьте логи бота).",
                ephemeral=True,
            )
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)

    @discord.ui.button(
        label="Победа синей команды", emoji="🔵", style=discord.ButtonStyle.primary, custom_id="mlbb_confirm_blue"
    )
    async def blue_wins(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._guarded_resolve(interaction, "BLUE")

    @discord.ui.button(
        label="Победа красной команды", emoji="🔴", style=discord.ButtonStyle.danger, custom_id="mlbb_confirm_red"
    )
    async def red_wins(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._guarded_resolve(interaction, "RED")


class Results(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # game_id -> список DM-сообщений с кнопками, чтобы отключить их
        # у ВСЕХ администраторов после того, как один из них подтвердит.
        self._pending_messages: dict[int, list[discord.Message]] = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        results_channel_id = config.guild_config(message.guild.id).results_channel_id
        if results_channel_id is None or message.channel.id != results_channel_id:
            return

        images = [a for a in message.attachments if a.content_type and a.content_type.startswith("image/")]
        if not images:
            return

        db = self.bot.db
        game = await db.get_active_game(message.guild.id)

        if game is None:
            return  # нет активной игры — сообщение не имеет отношения к результатам

        if game.status == "PENDING_CONFIRMATION":
            await message.reply(
                "⚠️ Результат этой игры уже отправлен и ожидает подтверждения администратора.",
                mention_author=False,
            )
            return

        if game.status != "PLAYING":
            return  # игра ещё в наборе/драфте — скриншот сейчас неуместен

        user = await db.get_user_by_discord_id(message.author.id)
        if user is None or not await db.is_player_in_game(game.game_id, user.id):
            await message.reply(
                "❌ Только участники текущей игры могут отправлять скриншот результата.",
                mention_author=False,
            )
            return

        # Fix (round 2, #1 — критический): атомарный переход PLAYING ->
        # PENDING_CONFIRMATION вместе с сохранением screenshot_message_id.
        # Раньше это были два раздельных запроса (set_screenshot +
        # update_game_status), и если два игрока отправляли скриншот
        # почти одновременно, оба успевали пройти проверку "status == PLAYING"
        # до того, как первый переключал статус — итог: два скриншота
        # считались валидными, второй мог перезаписать screenshot_message_id
        # первого, и администратору уходило неоднозначное подтверждение.
        # Теперь статус меняется только у того запроса, который физически
        # выполнится в БД первым (WHERE status = 'PLAYING'); rowcount == 0
        # у второго означает "кто-то уже успел раньше".
        accepted = await db.submit_screenshot_atomic(game.game_id, message.id)
        if not accepted:
            await message.reply(
                "⚠️ Результат этой игры уже отправлен другим игроком и ожидает подтверждения администратора.",
                mention_author=False,
            )
            return

        await message.reply(
            "✅ Скриншот получен. Результат отправлен администратору на подтверждение.",
            mention_author=False,
        )
        log.info("Игра #%s: получен скриншот от %s", game.game_id, message.author.id)

        await self._notify_admins(message.guild, game.game_id, images[0].url, message.jump_url)

    async def _notify_admins(
        self, guild: discord.Guild, game_id: int, screenshot_url: str | None, jump_url: str
    ):
        db = self.bot.db
        blue_team = await db.get_team_players(game_id, "BLUE")
        red_team = await db.get_team_players(game_id, "RED")

        embed = discord.Embed(
            title=f"🔒 Подтверждение результата — Игра #{game_id}",
            description="Этот интерфейс видишь только ты как администратор.",
            color=discord.Color.orange(),
        )
        embed.add_field(
            name="🔵 Синяя команда", value="\n".join(p.discord_username for p in blue_team), inline=True
        )
        embed.add_field(
            name="🔴 Красная команда", value="\n".join(p.discord_username for p in red_team), inline=True
        )
        embed.add_field(name="📸 Скриншот", value=f"[Перейти к сообщению]({jump_url})", inline=False)
        if screenshot_url:
            embed.set_image(url=screenshot_url)

        admins = [m for m in guild.members if not m.bot and is_admin(m)]
        sent: list[discord.Message] = []

        for admin in admins:
            try:
                dm = await admin.send(embed=embed, view=ConfirmResultView(self.bot, game_id))
                sent.append(dm)
            except discord.Forbidden:
                log.warning("Не удалось отправить DM администратору %s (закрыты личные сообщения)", admin)

        if not admins:
            log.warning("Не найдено ни одного администратора для подтверждения игры #%s", game_id)

        self._pending_messages[game_id] = sent

        from datetime import datetime, timezone

        await self.bot.db.set_confirm_notified_at(
            game_id, datetime.now(timezone.utc).isoformat()
        )

        # Fix (round 2, #10): если НИ ОДИН администратор не получил DM (все
        # закрыли личные сообщения, или их вообще нет на сервере), результат
        # рискует "зависнуть" в PENDING_CONFIRMATION — recover_pending_confirmations
        # после рестарта попробует снова, но с тем же успехом. Даём запасной
        # путь: явно предупреждаем в канале результатов и напоминаем про
        # /confirm_result — админ-команду, которая не зависит от DM.
        if admins and not sent:
            fallback_channel = self.bot.get_channel(config.guild_config(game.guild_id).results_channel_id) if config.guild_config(game.guild_id).results_channel_id else None
            if fallback_channel is not None:
                try:
                    await fallback_channel.send(
                        f"⚠️ Игра #{game_id}: не удалось отправить запрос на подтверждение результата ни "
                        f"одному администратору в личные сообщения (у всех закрыты DM). "
                        f"Используйте команду `/confirm_result game_id:{game_id} winner:...`, чтобы подтвердить вручную."
                    )
                except discord.HTTPException:
                    log.exception("Не удалось отправить резервное предупреждение по игре #%s в канал результатов", game_id)

    async def confirm_result_command(self, game_id: int, winner: str, confirmed_by: int) -> bool:
        """Fix (round 2, #10): используется резервной командой /confirm_result
        (cogs/admin.py) — тот же путь подтверждения, что и кнопки в DM,
        на случай если ни один администратор недоступен через личные
        сообщения."""
        return await self.finalize_result(game_id, winner, confirmed_by)

    async def finalize_result(self, game_id: int, winner: str, confirmed_by: int) -> bool:
        """Возвращает True, если именно этот вызов подтвердил результат
        (и, соответственно, начислил ELO); False, если результат уже был
        подтверждён раньше (защита от двойного начисления, п.30).

        Fix #2 (критическая ошибка): раньше фиксация победителя и начисление
        ELO десяти игрокам делались отдельными commit'ами один за другим —
        если бот/БД падали посередине, часть игроков получала ELO, часть
        нет, а игра уже была помечена COMPLETED. Теперь всё это — фиксация
        победителя + ELO всех 10 игроков + статистика + elo_history —
        выполняется в ОДНОЙ транзакции (finalize_game_result_atomic): либо
        применяется полностью, либо полностью откатывается."""
        db = self.bot.db

        blue_team = await db.get_team_players(game_id, "BLUE")
        red_team = await db.get_team_players(game_id, "RED")

        # Fix (round 2, #11): жёсткая проверка состава перед начислением ELO.
        # Ожидается ровно PLAYERS_PER_GAME // 2 игроков в каждой команде
        # (5+5 при стандартных настройках). Если из-за повреждения БД,
        # неудачного восстановления после рестарта или ручного вмешательства
        # состав окажется иным, начисление ELO кому попало было бы хуже,
        # чем отказ и явная ошибка администратору.
        expected_per_team = config.guild_config(game.guild_id).players_per_game // 2
        if len(blue_team) != expected_per_team or len(red_team) != expected_per_team:
            log.error(
                "Игра #%s: НЕКОРРЕКТНЫЙ состав команд перед начислением ELO "
                "(blue=%d, red=%d, ожидалось по %d) — начисление ОТМЕНЕНО, "
                "требуется ручная проверка администратором.",
                game_id, len(blue_team), len(red_team), expected_per_team,
            )
            return False

        winners = blue_team if winner == "BLUE" else red_team
        losers = red_team if winner == "BLUE" else blue_team

        from services.economy import roll_match_vc

        win_vc_map = {p.id: roll_match_vc(True) for p in winners}
        loss_vc_map = {p.id: roll_match_vc(False) for p in losers}

        won_race = await db.finalize_game_result_atomic(
            game_id,
            winner,
            confirmed_by,
            winner_ids=[p.id for p in winners],
            loser_ids=[p.id for p in losers],
            elo_win=config.ELO_WIN,
            elo_loss=config.ELO_LOSS,
            min_elo=config.MIN_ELO,
            win_vc_map=win_vc_map,
            loss_vc_map=loss_vc_map,
        )
        if not won_race:
            return False

        # Отключаем кнопки во всех остальных DM, отправленных другим админам —
        # чтобы никто не смог тыкнуть "уже неактуальную" кнопку и запутаться.
        for msg in self._pending_messages.pop(game_id, []):
            try:
                await msg.edit(view=None)
            except discord.HTTPException:
                pass

        details = await db.get_game_result_details(game_id)
        channel = self.bot.get_channel(config.guild_config(game.guild_id).results_channel_id) if config.guild_config(game.guild_id).results_channel_id else None
        if channel is not None:
            embed = build_result_announcement_embed(game_id, winner, details)
            await channel.send(embed=embed)
        else:
            log.warning("RESULTS_CHANNEL_ID не настроен — публичный результат игры #%s не отправлен", game_id)

        log.info("Игра #%s завершена. Победитель: %s. Подтвердил: %s", game_id, winner, confirmed_by)

        asyncio.create_task(self._cleanup_voice_channels(game_id))
        return True

    async def _cleanup_voice_channels(self, game_id: int):
        """Этап 42: удаление временных голосовых каналов через паузу
        после подтверждения результата.

        Fix #7: если удаление прошло успешно, сразу обнуляем ID каналов
        в БД (clear_voice_channels) — иначе cleanup_stale_voice_channels
        при следующем рестарте бота решит, что канал "остался", хотя он
        уже давно удалён.

        Fix (round 3, критический — требование п.4 ревью): "успешно
        удалён/подтверждённо отсутствует" теперь отслеживается ОТДЕЛЬНО
        для синего и красного канала. Раньше был один общий флаг
        any_deleted — если удалялся только один из двух каналов, второй
        (у которого реально произошёл сбой) всё равно терял свою ссылку
        в БД вместе с первым и "утекал" в гильдии навсегда. Теперь
        clear_voice_channels вызывается с точными clear_blue/clear_red,
        так что необработанный канал остаётся в БД и будет подхвачен
        cleanup_stale_voice_channels при следующем рестарте."""
        await asyncio.sleep(VOICE_CHANNEL_CLEANUP_DELAY)
        game = await self.bot.db.get_game(game_id)
        if game is None:
            return

        resolved = {"blue": False, "red": False}
        for label, channel_id in (
            ("blue", game.blue_voice_channel_id),
            ("red", game.red_voice_channel_id),
        ):
            if channel_id is None:
                resolved[label] = True  # нечего чистить — считаем "обработанным"
                continue
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                # Fix (round 2, #3 — критический): раньше "не найден в кеше"
                # тихо пропускался (continue) без попытки fetch. Если канал
                # уже был удалён вручную ДО этого момента, get_channel(...)
                # закономерно вернёт None — пробуем дополнительно fetch_channel:
                # Только NotFound подтверждает, что канала больше нет.
                # Forbidden/503 и прочие HTTP-ошибки не дают права стирать
                # его ID из БД: иначе канал может стать "сиротой" навсегда.
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.NotFound:
                    resolved[label] = True
                    continue
                except discord.HTTPException:
                    log.exception("Не удалось проверить голосовой канал %s (игра #%s)", channel_id, game_id)
                    continue
            try:
                await channel.delete(reason=f"MLBB game #{game_id} completed")
                resolved[label] = True
            except discord.HTTPException:
                log.exception("Не удалось удалить голосовой канал %s (игра #%s)", channel_id, game_id)
                resolved[label] = False

        if resolved["blue"] or resolved["red"]:
            await self.bot.db.clear_voice_channels(
                game_id, clear_blue=resolved["blue"], clear_red=resolved["red"]
            )

    async def cleanup_stale_voice_channels(self):
        """Fix #7: вызывается один раз при старте бота. Если бот был
        перезапущен ПОСЛЕ того, как игра завершилась, но ДО того, как
        сработал отложенный _cleanup_voice_channels (asyncio.create_task
        не переживает перезапуск процесса), временные голосовые каналы
        могли остаться в гильдии навсегда. Здесь мы находим все такие
        "осиротевшие" каналы по COMPLETED/CANCELLED играм и удаляем их."""
        db = self.bot.db
        games = await db.get_games_with_leftover_voice_channels()

        for game in games:
            # Fix (round 3, критический — требование п.4 ревью): как и в
            # _cleanup_voice_channels, отслеживаем успех/неудачу для КАЖДОГО
            # канала отдельно, а не одним общим флагом — иначе успешное
            # удаление одного канала стирало бы из БД ссылку и на второй,
            # даже если тот реально не удалился.
            resolved = {"blue": False, "red": False}
            for label, channel_id in (
                ("blue", game.blue_voice_channel_id),
                ("red", game.red_voice_channel_id),
            ):
                if channel_id is None:
                    resolved[label] = True
                    continue
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except discord.NotFound:
                        # Канал уже не существует (например, удалён вручную) — это ОК.
                        resolved[label] = True
                        continue
                    except discord.HTTPException:
                        log.exception(
                            "Не удалось проверить оставшийся голосовой канал %s (игра #%s)",
                            channel_id, game.game_id,
                        )
                        continue
                try:
                    await channel.delete(reason=f"MLBB game #{game.game_id}: cleanup после рестарта бота")
                    resolved[label] = True
                    log.info("Удалён оставшийся после рестарта голосовой канал %s (игра #%s)", channel_id, game.game_id)
                except discord.HTTPException:
                    log.exception("Не удалось удалить оставшийся голосовой канал %s (игра #%s)", channel_id, game.game_id)
                    resolved[label] = False

            if resolved["blue"] or resolved["red"]:
                await db.clear_voice_channels(
                    game.game_id, clear_blue=resolved["blue"], clear_red=resolved["red"]
                )

    async def recover_pending_confirmations(self):
        """Этап 39: если бот перезапустился, пока игра ждала подтверждения,
        старые кнопки в DM у администраторов уже "мертвы" (ConfirmResultView
        создавалась заново в памяти и не сохранялась). Поэтому просто
        отправляем администраторам новый запрос подтверждения — сам
        результат при этом не теряется, потому что screenshot_message_id
        и статус PENDING_CONFIRMATION уже сохранены в БД.

        Защита от crash-loop: если DM уже рассылались менее 10 минут назад,
        повторную рассылку пропускаем — админы могут подтвердить через
        /confirm_result, а спам в личку не уходит."""
        from datetime import datetime, timezone, timedelta

        db = self.bot.db
        games = await db.get_games_by_statuses(("PENDING_CONFIRMATION",))
        resend_cooldown = timedelta(minutes=10)

        for game in games:
            if game.screenshot_message_id is None or config.guild_config(game.guild_id).results_channel_id is None:
                log.warning("Игра #%s: нет данных для повторной отправки на подтверждение", game.game_id)
                continue

            if game.confirm_notified_at:
                try:
                    last = datetime.fromisoformat(game.confirm_notified_at)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) - last < resend_cooldown:
                        log.info(
                            "Игра #%s: повторная рассылка DM пропущена (последняя была %s, cooldown 10 мин)",
                            game.game_id, game.confirm_notified_at,
                        )
                        continue
                except ValueError:
                    pass

            guild = self.bot.get_guild(game.guild_id)
            channel = self.bot.get_channel(config.guild_config(game.guild_id).results_channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(config.guild_config(game.guild_id).results_channel_id)
                except discord.HTTPException:
                    guild = None  # форсируем пропуск ниже

            if guild is None or channel is None:
                log.warning("Игра #%s: гильдия или канал результатов недоступны, пропуск восстановления", game.game_id)
                continue

            try:
                message = await channel.fetch_message(game.screenshot_message_id)
            except discord.HTTPException:
                log.warning("Игра #%s: исходное сообщение со скриншотом не найдено", game.game_id)
                continue

            images = [a for a in message.attachments if a.content_type and a.content_type.startswith("image/")]
            screenshot_url = images[0].url if images else None

            await self._notify_admins(guild, game.game_id, screenshot_url, message.jump_url)
            log.info("Игра #%s: запрос на подтверждение отправлен администраторам повторно после рестарта", game.game_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Results(bot))
