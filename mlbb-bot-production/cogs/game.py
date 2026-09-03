"""
Ког игр (Этапы 8-19 + 39 из ТЗ).

Реализовано на этом этапе:
  - /game — администратор открывает набор (embed + кнопки).
  - 🟢 Вступить / 🔴 Покинуть — с проверками регистрации, голосового
    канала (если настроен) и повторного вступления.
  - Автозакрытие набора при 10/10 и перевод игры в статус DRAFT.
  - Случайный выбор двух капитанов (Этап 11).
  - Ручной драфт через Select-меню с фиксированной очерёдностью
    Синий/Красный и всеми проверками из п.15 (Этап 12-15 старого списка).
  - Завершение драфта -> статус PLAYING (Этап 16 старого списка).
  - Автоматическое создание двух временных голосовых каналов в заданной
    категории с Permission Overwrites и перемещением уже подключённых
    игроков (Этапы 13-15/17-19 ТЗ).
  - Восстановление WAITING/DRAFT игр после перезапуска бота через
    persistent views (custom_id + bot.add_view(view, message_id=...)),
    см. recover_games() (Этап 39).

НЕ реализовано в этом файле (сделано в других когах):
  - автоматический возврат отключившегося игрока в канал (п.20-21 ТЗ) —
    см. cogs/voice.py;
  - всё, что происходит после PLAYING (скриншот, подтверждение, ELO) —
    см. cogs/results.py;
  - удаление голосовых каналов по завершении игры (Этап 42) — тоже в
    cogs/results.py.
"""

import asyncio
import logging
import random

import discord
from discord import app_commands
from discord.ext import commands

import config
from database.models import User
from utils.helpers import build_draft_embed, build_lobby_embed, build_teams_ready_embed
from utils.permissions import is_admin

log = logging.getLogger("mlbb-bot.game")


class LobbyView(discord.ui.View):
    def __init__(self, bot: commands.Bot, game_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.game_id = game_id
        self.message: discord.Message | None = None

    async def _refresh_embed(self, closed: bool = False):
        if self.message is None:
            return
        players = await self.bot.db.get_game_players(self.game_id)
        embed = build_lobby_embed(self.game_id, players, closed=closed)
        await self.message.edit(embed=embed, view=self)
        return players

    async def _close_registration(self, players, captains: tuple[User, User] | None = None):
        db = self.bot.db

        # Fix #3 (сопутствующая защита): атомарный переход WAITING -> DRAFT.
        # Если счётчик игроков достиг 10 одновременно у двух конкурентных
        # обработчиков "Вступить", оба могли бы попытаться закрыть набор и
        # выбрать капитанов дважды. transition_game_status применяется
        # только если статус сейчас РОВНО WAITING — второй вызов получит
        # False и просто ничего не сделает.
        transitioned = await db.transition_game_status(self.game_id, "WAITING", "DRAFT")
        if not transitioned:
            return

        # BUG #5 fix: pick and persist captains RIGHT AFTER the DRAFT
        # transition, before any Discord API call. Previously captains were
        # only set after message.edit()/channel.send() succeeded, so an
        # HTTPException there (or self.message being None) left the game in
        # DRAFT with no captains and no way for recover_games() to rebuild
        # it — a permanently stuck game. Now the DB always has captains for
        # any DRAFT game, matching what _recover_draft() expects.
        blue_captain, red_captain = captains or tuple(random.sample(players, 2))
        captains_set = await db.set_captains(self.game_id, blue_captain.id, red_captain.id)
        if not captains_set:
            log.warning(
                "Игра #%s: set_captains rejected (status already left WAITING/DRAFT) — abort close",
                self.game_id,
            )
            return

        for child in self.children:
            child.disabled = True

        embed = build_lobby_embed(self.game_id, players, closed=True)
        if self.message is not None:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                log.exception("Игра #%s: не удалось обновить сообщение лобби после закрытия набора", self.game_id)
        log.info("Игра #%s: набор закрыт, статус DRAFT", self.game_id)

        if self.message is None:
            # BUG #5 fix: captains are already persisted above, so recovery
            # can still rebuild the draft once an admin/recover_games finds
            # a channel to post it in — we just can't announce it right now.
            log.warning(
                "Игра #%s: LobbyView.message отсутствует, драфт-сообщение не отправлено "
                "(капитаны уже сохранены, восстановление возможно вручную)",
                self.game_id,
            )
            return

        # BUG #5 fix: captains + game row are already durable at this point.
        # If Discord fails from here on, recover_games()/_recover_draft()
        # can still repost a fresh draft message using the saved captains
        # (see the draft_message_id-missing branch there), so we log and
        # stop instead of letting the exception bubble out of a button
        # interaction callback.
        try:
            announce = discord.Embed(
                title="👑 КОМАНДИРЫ",
                description=(
                    f"🔵 Синяя команда — **{blue_captain.discord_username}**\n"
                    f"🔴 Красная команда — **{red_captain.discord_username}**"
                ),
                color=discord.Color.gold(),
            )
            await self.message.channel.send(embed=announce)

            # --- Этап 12: запуск ручного драфта -------------------------------
            available = await db.get_available_players(self.game_id)
            draft_view = DraftView(self.bot, self.game_id, blue_captain, red_captain, available)
            draft_embed = build_draft_embed(
                self.game_id,
                blue_captain,
                blue_team=[],
                red_captain=red_captain,
                red_team=[],
                available=available,
                current_captain=blue_captain,
                current_team="BLUE",
            )
            draft_message = await self.message.channel.send(embed=draft_embed, view=draft_view)
            draft_view.message = draft_message
            await db.set_draft_message(self.game_id, draft_message.id)
            draft_view._restart_timeout()  # Fix #10: запускаем таймер на первый пик
        except discord.HTTPException:
            log.exception(
                "Игра #%s: не удалось отправить сообщение драфта — капитаны уже сохранены, "
                "recover_games() сможет пересоздать сообщение драфта при следующем запуске",
                self.game_id,
            )

    @discord.ui.button(label="Вступить", emoji="🟢", style=discord.ButtonStyle.success, custom_id="mlbb_lobby_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = self.bot.db
        game = await db.get_game(self.game_id)

        if game is None or game.status != "WAITING":
            await interaction.response.send_message("❌ Набор на эту игру уже закрыт.", ephemeral=True)
            return

        user = await db.get_user_by_discord_id(interaction.user.id)
        if user is None:
            await interaction.response.send_message(
                "❌ Сначала зарегистрируйся через `/register`.", ephemeral=True
            )
            return

        if config.guild_config(interaction.guild_id).required_voice_channel_id is not None:
            voice_state = interaction.user.voice
            in_required_channel = (
                voice_state is not None
                and voice_state.channel is not None
                and voice_state.channel.id == config.guild_config(interaction.guild_id).required_voice_channel_id
            )
            if not in_required_channel:
                await interaction.response.send_message(
                    "❌ Вы должны находиться в голосовом канале, чтобы вступить в игру.",
                    ephemeral=True,
                )
                return

        if await db.is_player_in_game(self.game_id, user.id):
            await interaction.response.send_message("⚠️ Вы уже участвуете в этой игре.", ephemeral=True)
            return

        # Fix #3 (критическая ошибка): раньше здесь было "проверить
        # количество -> добавить" двумя отдельными запросами, что при
        # одновременном нажатии двумя игроками могло дать 11/10 (оба видят
        # 9/10 и оба добавляются). add_game_player_if_space делает проверку
        # и вставку одним атомарным SQL-выражением.
        added = await db.add_game_player_if_space(self.game_id, user.id, config.guild_config(interaction.guild_id).players_per_game)
        if not added:
            # BUG #3/#13: could be 10/10 full, the game already left
            # WAITING, or the player is currently in this guild's matchmaking
            # queue (mutually exclusive with a lobby by design) — either way
            # this join must not succeed.
            in_queue = await db.get_queue_entries(interaction.guild_id)
            if any(e.player.id == user.id for e in in_queue):
                await interaction.response.send_message(
                    "❌ Вы сейчас в очереди `/queue` — сначала выйдите из неё (`/queue leave`), "
                    "чтобы вступить в это лобби.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "❌ Набор уже заполнен или закрыт — вы немного опоздали.", ephemeral=True
                )
            return

        await interaction.response.send_message("✅ Вы вступили в игру.", ephemeral=True)

        players = await self._refresh_embed()
        if players is not None and len(players) >= config.guild_config(interaction.guild_id).players_per_game:
            await self._close_registration(players)

    @discord.ui.button(label="Покинуть", emoji="🔴", style=discord.ButtonStyle.danger, custom_id="mlbb_lobby_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = self.bot.db
        game = await db.get_game(self.game_id)

        if game is None or game.status != "WAITING":
            await interaction.response.send_message(
                "❌ Набор уже закрыт, выйти через эту кнопку нельзя.", ephemeral=True
            )
            return

        user = await db.get_user_by_discord_id(interaction.user.id)
        if user is None or not await db.is_player_in_game(self.game_id, user.id):
            await interaction.response.send_message("⚠️ Вы не участвуете в этой игре.", ephemeral=True)
            return

        removed = await db.remove_game_player(self.game_id, user.id)
        if not removed:
            # BUG #3: набор успел закрыться (WAITING→DRAFT) между нашей
            # проверкой статуса выше и атомарным DELETE — не считаем это
            # успешным выходом и не трогаем embed драфта.
            await interaction.response.send_message(
                "❌ Набор уже закрыт, выйти через эту кнопку нельзя.", ephemeral=True
            )
            return
        await interaction.response.send_message("✅ Вы вышли из игры.", ephemeral=True)
        await self._refresh_embed()


class PlayerSelect(discord.ui.Select):
    """Select-меню доступных игроков. Пересобирается (options обновляются)
    после каждого пика — Discord не позволяет частично менять список опций,
    поэтому мы просто перезаписываем self.options целиком."""

    def __init__(self, draft_view: "DraftView"):
        self.draft_view = draft_view
        super().__init__(
            placeholder="Выберите игрока",
            min_values=1,
            max_values=1,
            options=draft_view._build_options(),
            custom_id="mlbb_draft_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await self.draft_view.handle_pick(interaction, self.values[0])


class DraftView(discord.ui.View):
    """Ручной драфт капитанов (ТЗ, п.12-16).

    Очерёдность фиксированная: Синий, Красный, Синий, Красный... — 8 пиков
    всего (по 4 на капитана), реализовано через self.pick_index % 2.
    """

    TOTAL_PICKS = 8
    PICK_TIMEOUT_SECONDS = 60  # Fix #10: капитан не может "заморозить" драфт навсегда

    def __init__(
        self,
        bot: commands.Bot,
        game_id: int,
        blue_captain: User,
        red_captain: User,
        available: list[User],
    ):
        super().__init__(timeout=None)
        self.bot = bot
        self.game_id = game_id
        self.blue_captain = blue_captain
        self.red_captain = red_captain
        self.available = list(available)
        self.blue_team: list[User] = []
        self.red_team: list[User] = []
        self.pick_index = 0
        self.message: discord.Message | None = None

        # Fix #4: защищает handle_pick от гонки, когда капитан кликает
        # дважды очень быстро (или два interaction обрабатываются
        # параллельно) — без этой блокировки оба клика могли одновременно
        # увидеть игрока в self.available ещё до того, как первый успеет
        # его убрать.
        self._lock = asyncio.Lock()
        # _finalize can be reached from a captain click and a timeout at
        # nearly the same time.  The selection lock is released before
        # finalization, so keep a separate one-shot guard around the
        # side-effecting voice-channel setup.
        self._finalizing = False

        # Fix #10: таймаут выбора капитана, чтобы драфт не завис навсегда,
        # если капитан ушёл спать. _pick_generation используется, чтобы
        # "устаревший" таймер (запущенный до последнего пика) не сработал
        # по ошибке после того, как выбор уже был сделан.
        self._pick_generation = 0
        self._timeout_task: asyncio.Task | None = None

        self.select = PlayerSelect(self)
        self.add_item(self.select)

    def _build_options(self) -> list[discord.SelectOption]:
        return [
            discord.SelectOption(label=p.discord_username, value=str(p.id))
            for p in self.available
        ] or [discord.SelectOption(label="—", value="none")]

    def current_team(self) -> str:
        return "BLUE" if self.pick_index % 2 == 0 else "RED"

    def current_captain(self) -> User:
        return self.blue_captain if self.current_team() == "BLUE" else self.red_captain

    async def handle_pick(self, interaction: discord.Interaction, picked_value: str):
        # Fix #4: вся логика пика — под блокировкой, чтобы два почти
        # одновременных клика (или два interaction в обработке параллельно)
        # не могли пройти проверки одновременно и выбрать одного и того же
        # игрока до того, как первый пик уберёт его из self.available.
        async with self._lock:
            game = await self.bot.db.get_game(self.game_id)
            if game is None or game.status != "DRAFT":
                await interaction.response.send_message(
                    "❌ Эта игра больше не активна (возможно, была отменена администратором).",
                    ephemeral=True,
                )
                return

            current_captain = self.current_captain()

            # --- Этап 15: ограничения выбора --------------------------------
            if interaction.user.id not in (self.blue_captain.discord_id, self.red_captain.discord_id):
                await interaction.response.send_message("❌ Вы не можете выбирать игроков.", ephemeral=True)
                return

            if interaction.user.id != current_captain.discord_id:
                await interaction.response.send_message("❌ Сейчас выбирает другой командир.", ephemeral=True)
                return

            try:
                picked_id = int(picked_value)
            except ValueError:
                picked_id = None

            player = next((p for p in self.available if p.id == picked_id), None)
            if player is None:
                await interaction.response.send_message(
                    "❌ Этот игрок уже выбран или недоступен для выбора.", ephemeral=True
                )
                return

            team = self.current_team()

            # Fix #4: дополнительная атомарная проверка на уровне БД
            # (team IS NULL) — даже если бы блокировка в памяти была
            # каким-то образом обойдена (например, восстановление после
            # рестарта создало второй экземпляр DraftView), два разных
            # DraftView не смогут оба назначить одного и того же игрока.
            assigned = await self.bot.db.assign_team_if_available(self.game_id, player.id, team)
            if not assigned:
                self.available = [p for p in self.available if p.id != player.id]
                await interaction.response.send_message(
                    "❌ Этот игрок уже выбран или недоступен для выбора.", ephemeral=True
                )
                await self._refresh()
                return

            self.available.remove(player)
            if team == "BLUE":
                self.blue_team.append(player)
            else:
                self.red_team.append(player)

            self.pick_index += 1

            team_label = "🔵 синюю" if team == "BLUE" else "🔴 красную"
            await interaction.response.send_message(
                f"✅ **{player.discord_username}** выбран в {team_label} команду.", ephemeral=True
            )

            if self.pick_index >= self.TOTAL_PICKS:
                self._cancel_timeout()
                await self._finalize()
            else:
                await self._refresh()
                self._restart_timeout()

    def _cancel_timeout(self):
        if self._timeout_task is not None and not self._timeout_task.done():
            self._timeout_task.cancel()
        self._timeout_task = None

    def _restart_timeout(self, remaining_seconds: float | None = None):
        """Fix #10: (пере)запускает таймер ожидания хода текущего капитана.
        Вызывается после каждого успешного пика (и при первом запуске
        драфта/восстановлении после рестарта).

        remaining_seconds — если задан (восстановление после рестарта),
        ждём только остаток, а не полные PICK_TIMEOUT_SECONDS."""
        self._cancel_timeout()
        self._pick_generation += 1
        seconds = self.PICK_TIMEOUT_SECONDS if remaining_seconds is None else max(0.0, remaining_seconds)
        self._timeout_task = asyncio.create_task(
            self._timeout_watch(self._pick_generation, seconds)
        )
        # Сохраняем абсолютный дедлайн в БД, чтобы рестарт не обнулял таймер.
        asyncio.create_task(self._persist_pick_deadline(seconds))

    async def _persist_pick_deadline(self, seconds: float) -> None:
        from datetime import datetime, timedelta, timezone

        deadline = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
        try:
            await self.bot.db.set_draft_pick_deadline(self.game_id, deadline)
        except Exception:
            log.exception("Не удалось сохранить draft_pick_deadline для игры #%s", self.game_id)

    async def _timeout_watch(self, generation: int, seconds: float):
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        if generation != self._pick_generation:
            return  # успели сделать пик, этот таймер уже неактуален
        await self._auto_pick_on_timeout()

    async def _auto_pick_on_timeout(self):
        """Fix #10: если капитан не выбрал игрока за отведённое время,
        бот автоматически выбирает случайного доступного игрока за него —
        чтобы драфт не завис навсегда, если капитан отошёл/уснул."""
        async with self._lock:
            game = await self.bot.db.get_game(self.game_id)
            if game is None or game.status != "DRAFT" or not self.available:
                return

            team = self.current_team()
            captain = self.current_captain()

            # BUG #9 fix: assign_team_if_available can legitimately fail if
            # this player was just picked by the manual click that raced
            # this very timeout (or, via BUG #3's leave-race, dropped out
            # of the pool entirely). The old code just refreshed and
            # returned WITHOUT restarting the timer — the draft would then
            # be stuck in DRAFT forever with no running timeout and no way
            # for a captain to be nudged again. Retry against the rest of
            # the local pool first; if everyone in it turns out to be
            # already taken, fall back to a DB-driven read of the actual
            # available players before giving up.
            player = None
            assigned = False
            candidates = list(self.available)
            random.shuffle(candidates)
            for candidate in candidates:
                if await self.bot.db.assign_team_if_available(self.game_id, candidate.id, team):
                    player = candidate
                    assigned = True
                    break
                self.available = [p for p in self.available if p.id != candidate.id]

            if not assigned:
                # Local pool was entirely stale. Re-sync from DB once and
                # try again — this also self-heals the leave-race case
                # where a captain-elect player disappeared from
                # game_players mid-draft.
                self.available = await self.bot.db.get_available_players(self.game_id)
                for candidate in self.available:
                    if await self.bot.db.assign_team_if_available(self.game_id, candidate.id, team):
                        player = candidate
                        assigned = True
                        break

            if not assigned:
                # Truly nobody left to assign (e.g. everyone still
                # available left the game mid-draft, BUG #3 edge case).
                # We cannot silently freeze: restart the timer so the
                # draft keeps getting nudged and an admin/captain has a
                # visible, recoverable state instead of a permanently
                # stuck DRAFT with no running timeout.
                await self._refresh()
                log.error(
                    "Игра #%s: авто-пик не смог назначить ни одного игрока (available пуст/устарел) — "
                    "перезапускаю таймер, требуется проверка администратором",
                    self.game_id,
                )
                self._restart_timeout()
                return

            self.available.remove(player)
            if team == "BLUE":
                self.blue_team.append(player)
            else:
                self.red_team.append(player)
            self.pick_index += 1

            log.info(
                "Игра #%s: капитан %s не выбрал за %sс — автоматически выбран %s",
                self.game_id, captain.discord_username, self.PICK_TIMEOUT_SECONDS, player.discord_username,
            )

            if self.message is not None:
                team_label = "🔵 синюю" if team == "BLUE" else "🔴 красную"
                try:
                    await self.message.channel.send(
                        f"⏰ **{captain.discord_username}** не выбрал игрока за {self.PICK_TIMEOUT_SECONDS} секунд. "
                        f"Бот автоматически выбрал **{player.discord_username}** в {team_label} команду."
                    )
                except discord.HTTPException:
                    pass

            if self.pick_index >= self.TOTAL_PICKS:
                self._cancel_timeout()
                await self._finalize()
            else:
                await self._refresh()
                self._restart_timeout()

    async def _refresh(self):
        if self.message is None:
            return
        self.select.options = self._build_options()
        embed = build_draft_embed(
            self.game_id,
            self.blue_captain,
            self.blue_team,
            self.red_captain,
            self.red_team,
            self.available,
            current_captain=self.current_captain(),
            current_team=self.current_team(),
        )
        await self.message.edit(embed=embed, view=self)

    async def _finalize(self):
        if self._finalizing:
            return
        self._finalizing = True
        self._cancel_timeout()
        for child in self.children:
            child.disabled = True

        # Fix (round 2, #5 — важно): раньше игра переводилась в PLAYING
        # ДО попытки создать голосовые каналы — если Discord не давал их
        # создать, игра всё равно оставалась PLAYING без единого войса.
        # Теперь (Вариант А из ревью) сначала пытаемся создать войсы, ПОКА
        # статус ещё DRAFT, и переводим в PLAYING только при успехе —
        # либо войсы реально созданы и сохранены, либо они осознанно не
        # настроены (TEAM_VOICE_CATEGORY_ID is None), но не в случае
        # настоящего сбоя Discord API/БД.
        blue_channel, red_channel, voice_ok = await self._setup_voice_channels()

        if not voice_ok:
            # _setup_voice_channels уже уведомил администраторов о причине
            # через _notify_voice_setup_failure. Статус остаётся DRAFT —
            # драфт уже полностью укомплектован (8/8 пиков сделано), но
            # игра физически не может стартовать без войсов, пока
            # администратор не разберётся с проблемой (права бота, права
            # категории и т.д.) и не отменит/не перезапустит создание
            # войсов вручную.
            if self.message is not None:
                try:
                    # Отражаем в самом сообщении драфта, что кнопки
                    # действительно отключены (self.children уже помечены
                    # disabled чуть выше) — иначе игрокам казалось бы, что
                    # ничего не произошло.
                    await self.message.edit(view=self)
                    await self.message.channel.send(
                        f"⚠️ Игра #{self.game_id}: составы команд собраны, но старт отложен из-за проблемы "
                        f"с голосовыми каналами (см. сообщение выше). Обратитесь к администратору — "
                        f"можно отменить игру через /cancel_game."
                    )
                except discord.HTTPException:
                    pass
            log.error("Игра #%s: PLAYING отложен — не удалось гарантированно создать голосовые каналы", self.game_id)
            return

        # Fix #4 (сопутствующая защита): атомарный переход DRAFT -> PLAYING —
        # на случай, если _finalize по какой-то причине вызван дважды
        # (например, авто-пик по таймеру и ручной пик пересеклись), второй
        # вызов не запустит создание голосовых каналов повторно.
        transitioned = await self.bot.db.transition_game_status(self.game_id, "DRAFT", "PLAYING")
        if not transitioned:
            # The game can be cancelled while Discord channels are being
            # created.  Do not leave newly-created channels behind merely
            # because the final status CAS lost that race.
            cleared_blue = cleared_red = False
            for channel, is_blue in ((blue_channel, True), (red_channel, False)):
                if channel is None:
                    continue
                try:
                    await channel.delete(reason=f"MLBB game #{self.game_id}: draft did not transition to PLAYING")
                    if is_blue:
                        cleared_blue = True
                    else:
                        cleared_red = True
                except discord.HTTPException:
                    log.exception("Не удалось удалить канал %s после отменённого старта игры #%s", channel.id, self.game_id)
            if cleared_blue or cleared_red:
                await self.bot.db.clear_voice_channels(
                    self.game_id, clear_blue=cleared_blue, clear_red=cleared_red
                )
            return

        embed = build_teams_ready_embed(
            self.game_id,
            self.blue_captain,
            self.blue_team,
            self.red_captain,
            self.red_team,
            blue_channel=blue_channel,
            red_channel=red_channel,
        )
        if self.message is not None:
            await self.message.edit(embed=embed, view=self)
        log.info("Игра #%s: драфт завершён, статус PLAYING", self.game_id)

    async def _setup_voice_channels(self):
        """Этапы 13-15: создание временных голосовых каналов команд,
        Permission Overwrites и перемещение уже подключённых игроков.

        Если TEAM_VOICE_CATEGORY_ID не настроен или у бота не хватает прав,
        мы не роняем игру — просто логируем и продолжаем без каналов
        (игроки смогут договориться о голосовом канале сами)."""
        guild = self.message.guild if self.message is not None else None
        if guild is None:
            return None, None, True

        if config.guild_config(guild.id).team_voice_category_id is None:
            log.warning("TEAM_VOICE_CATEGORY_ID не настроен — голосовые каналы не созданы (игра #%s)", self.game_id)
            return None, None, True

        # BUG #4 fix: _finalize() can legitimately run twice for the same
        # game (manual pick racing the auto-pick timeout, or a restart
        # recovering a DRAFT that already reached 8/8 picks — see
        # _recover_draft). Previously this always created a brand-new pair
        # of voice channels, orphaning the first pair (their IDs get
        # overwritten in the DB, so cleanup could never find them again).
        # Reload the game and, if voice IDs are already persisted, try to
        # reuse those real Discord channels instead of creating new ones.
        current = await self.bot.db.get_game(self.game_id)
        if current is not None and current.blue_voice_channel_id and current.red_voice_channel_id:
            existing_blue = guild.get_channel(current.blue_voice_channel_id)
            existing_red = guild.get_channel(current.red_voice_channel_id)
            if not isinstance(existing_blue, discord.VoiceChannel):
                try:
                    existing_blue = await guild.fetch_channel(current.blue_voice_channel_id)
                except discord.HTTPException:
                    existing_blue = None
            if not isinstance(existing_red, discord.VoiceChannel):
                try:
                    existing_red = await guild.fetch_channel(current.red_voice_channel_id)
                except discord.HTTPException:
                    existing_red = None
            if isinstance(existing_blue, discord.VoiceChannel) and isinstance(existing_red, discord.VoiceChannel):
                log.info(
                    "Игра #%s: повторный _finalize — переиспользую уже существующие голосовые каналы",
                    self.game_id,
                )
                return existing_blue, existing_red, True
            # One or both channels are gone (deleted manually, etc). Fall
            # through and create a fresh pair rather than getting stuck.
            log.warning(
                "Игра #%s: сохранённые voice ID не соответствуют существующим каналам — создаю новые",
                self.game_id,
            )

        category = guild.get_channel(config.guild_config(guild.id).team_voice_category_id)
        if not isinstance(category, discord.CategoryChannel):
            log.warning(
                "TEAM_VOICE_CATEGORY_ID=%s не является категорией или не найден (игра #%s)",
                config.guild_config(guild.id).team_voice_category_id,
                self.game_id,
            )
            await self._notify_voice_setup_failure(
                guild, "настроенная категория голосовых каналов не найдена или имеет неверный тип"
            )
            return None, None, False

        admin_role = guild.get_role(config.guild_config(guild.id).admin_role_id) if config.guild_config(guild.id).admin_role_id else None

        def base_overwrites() -> dict:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, move_members=True),
            }
            if admin_role is not None:
                overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, connect=True)
            return overwrites

        blue_overwrites = base_overwrites()
        for user in [self.blue_captain, *self.blue_team]:
            member = guild.get_member(user.discord_id)
            if member is not None:
                blue_overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True)

        red_overwrites = base_overwrites()
        for user in [self.red_captain, *self.red_team]:
            member = guild.get_member(user.discord_id)
            if member is not None:
                red_overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True)

        # Fix #5 (критическая ошибка): раньше оба канала создавались одним
        # блоком try/except discord.Forbidden — если Blue создался, а Red
        # упал (в том числе по discord.HTTPException, которую раньше вообще
        # не ловили — например, временный сбой Discord API), Blue оставался
        # висеть "осиротевшим", а игра всё равно переходила в PLAYING без
        # второго канала. Теперь создаём каналы по отдельности, при неудаче
        # на втором шаге удаляем уже созданный первый канал и явно
        # уведомляем администраторов о проблеме.
        #
        # Возвращаемое значение — (blue, red, ok). ok=True означает "можно
        # переводить игру в PLAYING": либо оба канала успешно созданы и
        # сохранены в БД, либо голосовые каналы вообще не настроены
        # (TEAM_VOICE_CATEGORY_ID is None) — это осознанная конфигурация,
        # а не сбой. ok=False — голосовые каналы БЫЛИ настроены, но что-то
        # реально сломалось (Discord API, сохранение в БД) — в этом случае
        # вызывающий код (Fix #5, round 2) не должен переводить игру в
        # PLAYING, чтобы не оставить игроков без назначенных войсов молча.
        blue_channel: discord.VoiceChannel | None = None
        red_channel: discord.VoiceChannel | None = None
        try:
            blue_channel = await guild.create_voice_channel(
                name=f"🔵・Blue Team #{self.game_id}",
                category=category,
                overwrites=blue_overwrites,
                reason=f"MLBB game #{self.game_id}",
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Не удалось создать синий голосовой канал (игра #%s)", self.game_id)
            await self._notify_voice_setup_failure(guild, "не удалось создать голосовой канал синей команды")
            return None, None, False

        try:
            red_channel = await guild.create_voice_channel(
                name=f"🔴・Red Team #{self.game_id}",
                category=category,
                overwrites=red_overwrites,
                reason=f"MLBB game #{self.game_id}",
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Не удалось создать красный голосовой канал (игра #%s)", self.game_id)
            # Синий канал уже создан — не оставляем его "осиротевшим".
            try:
                await blue_channel.delete(reason=f"MLBB game #{self.game_id}: откат из-за ошибки создания второго канала")
            except discord.HTTPException:
                log.exception("Не удалось откатить (удалить) синий канал после сбоя создания красного (игра #%s)", self.game_id)
            await self._notify_voice_setup_failure(
                guild, "синий канал создан, но не удалось создать красный — оба канала отменены"
            )
            return None, None, False

        # Fix (round 2, #6): оба канала уже реально существуют в Discord —
        # если сохранение их ID в БД упадёт (например, диск/SQLite),
        # получится "живые" каналы без ссылок в БД, которые cleanup после
        # рестарта не найдёт и не удалит никогда. Поэтому при ошибке
        # сохранения откатываем (удаляем) оба только что созданных канала —
        # тогда в Discord не останется ничего, что не отражено в БД.
        try:
            await self.bot.db.set_voice_channels(self.game_id, blue_channel.id, red_channel.id)
        except Exception:
            log.exception("Не удалось сохранить ID голосовых каналов в БД (игра #%s) — откатываю оба канала", self.game_id)
            for ch in (blue_channel, red_channel):
                try:
                    await ch.delete(reason=f"MLBB game #{self.game_id}: откат из-за сбоя сохранения в БД")
                except discord.HTTPException:
                    log.exception("Не удалось откатить (удалить) канал %s после сбоя сохранения в БД (игра #%s)", ch.id, self.game_id)
            await self._notify_voice_setup_failure(
                guild, "каналы созданы, но не удалось сохранить их в базе данных — оба канала отменены"
            )
            return None, None, False

        log.info(
            "Игра #%s: созданы голосовые каналы blue=%s red=%s",
            self.game_id, blue_channel.id, red_channel.id,
        )

        failed_blue = await self._move_team(guild, [self.blue_captain, *self.blue_team], blue_channel)
        failed_red = await self._move_team(guild, [self.red_captain, *self.red_team], red_channel)

        # Fix #11: если бота не хватило прав переместить кого-то (например,
        # нет Move Members), явно сообщаем об этом администраторам вместо
        # тихого лога — игра продолжается, но игрок должен зайти в канал сам.
        # Это НЕ считается критическим сбоем (ok=True) — оба канала созданы
        # и сохранены, просто кому-то придётся зайти вручную.
        failed_all = failed_blue + failed_red
        if failed_all:
            names = ", ".join(m.discord_username for m in failed_all)
            await self._notify_voice_setup_failure(
                guild, f"не удалось автоматически переместить в голосовой канал: {names}. Игрокам нужно зайти самим."
            )

        return blue_channel, red_channel, True

    async def _notify_voice_setup_failure(self, guild: discord.Guild, reason: str):
        """Fix #5/#11: короткое уведомление в том же канале, где идёт
        драфт/лобби, чтобы администратор сразу увидел проблему с войсами,
        а не искал её в логах."""
        if self.message is None:
            return
        try:
            await self.message.channel.send(
                f"⚠️ Игра #{self.game_id}: проблема с голосовыми каналами — {reason}."
            )
        except discord.HTTPException:
            log.exception("Не удалось отправить уведомление о проблеме с войсами (игра #%s)", self.game_id)

    async def _move_team(self, guild: discord.Guild, team_users: list[User], channel: discord.VoiceChannel) -> list[User]:
        """Перемещает уже подключённых к голосу игроков в их командный канал.
        Тех, кто не в голосе, не трогаем — они смогут зайти сами
        (права доступа уже выданы, см. ТЗ п.19-20).

        Fix #11: возвращает список игроков, которых переместить не
        удалось (были в голосе, но move_to не прошёл) — чтобы вызывающий
        код мог уведомить администратора."""
        failed: list[User] = []
        for user in team_users:
            member = guild.get_member(user.discord_id)
            if member is None or member.voice is None or member.voice.channel is None:
                continue
            try:
                await member.move_to(channel, reason=f"MLBB game #{self.game_id}: team assignment")
            except discord.Forbidden:
                log.warning("Нет прав переместить %s в %s (игра #%s)", member, channel, self.game_id)
                failed.append(user)
            except discord.HTTPException:
                log.exception("Не удалось переместить %s (игра #%s)", member, self.game_id)
                failed.append(user)
        return failed


class Game(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="game", description="Открыть набор игроков на игру 5×5")
    @app_commands.default_permissions(manage_guild=True)
    async def game(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Эта команда работает только на сервере.", ephemeral=True)
            return

        # Двойная проверка прав (ТЗ, п.44): default_permissions скрывает команду
        # в UI, но не гарантирует защиту, если права на сервере настроены нестандартно.
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Только администратор может запускать набор.", ephemeral=True
            )
            return

        existing = await self.bot.db.get_active_game(interaction.guild_id)
        if existing is not None:
            await interaction.response.send_message(
                "❌ На сервере уже проходит активная игра.", ephemeral=True
            )
            return

        # Fix (round 2, критический — "Защитить create_game() от двух
        # одновременных /game"): "проверить -> создать" двумя раздельными
        # запросами оставляло гонку — если два администратора вызывали
        # /game почти одновременно, оба могли увидеть existing=None и оба
        # создать свою активную игру на одном guild_id. create_game_if_none_active
        # полагается на уникальный частичный индекс idx_games_one_active_per_guild
        # в БД (см. database.py, _migrate): вставка второй активной игры
        # для того же guild_id физически невозможна, и метод в этом случае
        # вернёт None вместо созданной игры.
        game = await self.bot.db.create_game_if_none_active(interaction.guild_id)
        if game is None:
            await interaction.response.send_message(
                "❌ На сервере уже проходит активная игра (создана буквально мгновением раньше).",
                ephemeral=True,
            )
            return

        embed = build_lobby_embed(game.game_id, players=[])
        view = LobbyView(self.bot, game.game_id)

        # Fix (аудит, критический — тот же класс проблемы, что и в
        # cogs/queue.py._try_start_match): игра уже создана в БД
        # (create_game_if_none_active) и занимает единственный слот
        # "активной игры на сервер". Если отправка сообщения лобби,
        # получение interaction.original_response() или сохранение
        # lobby_message_id упадёт (discord.HTTPException, обрыв связи,
        # ошибка БД), игра осталась бы WAITING без lobby_channel_id/
        # lobby_message_id навсегда — recover_games() не смог бы её
        # восстановить (нет канала), а create_game_if_none_active больше
        # никогда не пропустит новый /game на этом сервере. Пустая
        # WAITING-игра без единого игрока безопасно отменяется целиком.
        try:
            await interaction.response.send_message(embed=embed, view=view)
            view.message = await interaction.original_response()
            await self.bot.db.set_lobby_message(game.game_id, view.message.channel.id, view.message.id)
        except Exception:
            log.exception(
                "Игра #%s: не удалось создать сообщение лобби — отменяю пустую игру, "
                "чтобы не блокировать /game на сервере %s",
                game.game_id, interaction.guild_id,
            )
            await self.bot.db.cancel_active_game_atomic(game.game_id)
            # Пользовательское сообщение об ошибке и логирование берёт на
            # себя общий обработчик bot.on_app_command_error (см. bot.py) —
            # он умеет корректно ответить и через response, и через followup
            # в зависимости от того, на каком шаге упало исключение выше.
            raise

        log.info("Создана игра #%s на сервере %s", game.game_id, interaction.guild_id)

    async def recover_games(self):
        """Этап 39: восстановление игр, которые были активны на момент
        остановки/перезапуска бота.

        Для WAITING — просто переподключаем LobbyView к существующему
        сообщению (участники уже в БД, ничего пересчитывать не нужно).

        Для DRAFT — восстанавливаем состояние пика из game_players:
        pick_index вычисляется как количество уже выбранных НЕ-капитанов,
        поэтому очередность (Синий/Красный) продолжится с того же места.

        Игры в PLAYING/PENDING_CONFIRMATION восстанавливать не нужно —
        приём скриншота и подтверждение результата не зависят от
        in-memory view, они целиком читают состояние из БД при каждом
        обращении (см. cogs/results.py)."""
        db = self.bot.db
        games = await db.get_games_by_statuses(("WAITING", "DRAFT"))

        for game in games:
            if game.lobby_channel_id is None:
                # Fix (аудит, критический — п.2/п.11 ревью): игра без
                # lobby_channel_id — это "осиротевшая" игра: она заняла
                # единственный слот активной игры на сервере (или, для
                # DRAFT из очереди, уже сняла игроков с очереди), но
                # никогда не получила ни одного Discord-сообщения — значит
                # физически недостижима и никогда не станет достижимой.
                # Раньше она оставалась висеть в этом статусе НАВСЕГДА:
                # ни /game, ни новый матч из очереди не могли начаться на
                # этом сервере, пока администратор не находил и не отменял
                # её вручную по логам. Отменяем автоматически; если это
                # был матч из очереди (есть игроки), возвращаем их туда.
                log.warning(
                    "Игра #%s (%s): нет сохранённого канала (осиротела до объявления) — "
                    "отменяю автоматически, чтобы не блокировать новые игры на сервере %s",
                    game.game_id, game.status, game.guild_id,
                )
                players = await db.get_game_players(game.game_id)
                if players:
                    await db.cancel_and_requeue_atomic(
                        game.game_id, game.guild_id, [p.id for p in players]
                    )
                else:
                    await db.cancel_active_game_atomic(game.game_id)
                continue

            channel = self.bot.get_channel(game.lobby_channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(game.lobby_channel_id)
                except discord.HTTPException:
                    log.warning("Игра #%s: канал %s недоступен, восстановление невозможно", game.game_id, game.lobby_channel_id)
                    continue

            if game.status == "WAITING":
                await self._recover_lobby(channel, game)
            elif game.status == "DRAFT":
                await self._recover_draft(channel, game)

    async def _recover_lobby(self, channel: discord.abc.Messageable, game):
        if game.lobby_message_id is None:
            log.warning("Игра #%s: нет lobby_message_id, восстановление лобби невозможно", game.game_id)
            return
        try:
            message = await channel.fetch_message(game.lobby_message_id)
        except discord.HTTPException:
            log.warning("Игра #%s: сообщение лобби не найдено, восстановление невозможно", game.game_id)
            return

        view = LobbyView(self.bot, game.game_id)
        view.message = message
        self.bot.add_view(view, message_id=message.id)
        log.info("Восстановлено лобби игры #%s (WAITING)", game.game_id)

        # Если бот упал между коммитом 10-го игрока и _close_registration,
        # в БД остаётся WAITING с полным составом — доводим закрытие набора.
        players = await self.bot.db.get_game_players(game.game_id)
        if len(players) >= config.guild_config(interaction.guild_id).players_per_game:
            log.warning(
                "Игра #%s: восстановлено лобби, уже заполненное (%d/%d) — довершаю закрытие набора",
                game.game_id, len(players), config.guild_config(interaction.guild_id).players_per_game,
            )
            await view._close_registration(players)

    async def _recover_draft(self, channel: discord.abc.Messageable, game):
        db = self.bot.db
        if game.blue_captain is None or game.red_captain is None:
            log.warning("Игра #%s: недостаточно данных для восстановления драфта (нет капитанов)", game.game_id)
            return

        blue_captain = await db.get_user_by_id(game.blue_captain)
        red_captain = await db.get_user_by_id(game.red_captain)
        if blue_captain is None or red_captain is None:
            log.warning("Игра #%s: капитаны не найдены в БД, восстановление невозможно", game.game_id)
            return

        blue_team_all = await db.get_team_players(game.game_id, "BLUE")
        red_team_all = await db.get_team_players(game.game_id, "RED")
        blue_team = [p for p in blue_team_all if p.id != blue_captain.id]
        red_team = [p for p in red_team_all if p.id != red_captain.id]
        available = await db.get_available_players(game.game_id)

        message: discord.Message | None = None
        if game.draft_message_id is not None:
            try:
                message = await channel.fetch_message(game.draft_message_id)
            except discord.HTTPException:
                log.warning(
                    "Игра #%s: сообщение драфта %s не найдено — попробую отправить новое (BUG #5 fix)",
                    game.game_id, game.draft_message_id,
                )
                message = None

        if message is None:
            # BUG #5 fix: captains are always persisted before the draft
            # message is sent (see _close_registration), so a crash/API
            # failure between those two steps must not strand the game —
            # repost a fresh draft message reflecting the current picks.
            log.warning(
                "Игра #%s: нет сохранённого сообщения драфта — отправляю новое", game.game_id,
            )
            draft_embed = build_draft_embed(
                game.game_id,
                blue_captain,
                blue_team,
                red_captain,
                red_team,
                available,
                current_captain=blue_captain if len(blue_team) <= len(red_team) else red_captain,
                current_team="BLUE" if len(blue_team) <= len(red_team) else "RED",
            )
            try:
                message = await channel.send(
                    content=f"🔁 Восстановление после перезапуска (игра #{game.game_id}):",
                    embed=draft_embed,
                )
                await db.set_draft_message(game.game_id, message.id)
            except discord.HTTPException:
                log.exception(
                    "Игра #%s: не удалось отправить восстановленное сообщение драфта", game.game_id,
                )
                return

        draft_view = DraftView(self.bot, game.game_id, blue_captain, red_captain, available)
        draft_view.blue_team = blue_team
        draft_view.red_team = red_team
        draft_view.pick_index = len(blue_team) + len(red_team)
        draft_view.select.options = draft_view._build_options()
        draft_view.message = message

        # Если бот упал после последнего пика, но до _finalize (создание войсов /
        # переход в PLAYING), в БД остаётся DRAFT с полным составом.
        # _auto_pick_on_timeout при пустом available просто выходит — доводим сами.
        if draft_view.pick_index >= DraftView.TOTAL_PICKS:
            log.warning(
                "Игра #%s: восстановлен полностью укомплектованный, но не финализированный драфт — довершаю",
                game.game_id,
            )
            self.bot.add_view(draft_view, message_id=message.id)
            await draft_view._finalize()
            return

        self.bot.add_view(draft_view, message_id=message.id)

        # Восстанавливаем остаток таймера пика из БД, а не полные 60 секунд.
        remaining: float | None = None
        if game.draft_pick_deadline:
            from datetime import datetime, timezone

            try:
                deadline = datetime.fromisoformat(game.draft_pick_deadline)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            except ValueError:
                remaining = None
        draft_view._restart_timeout(remaining_seconds=remaining)
        log.info(
            "Восстановлен драфт игры #%s (сделано пиков: %s/%s, остаток таймера: %sс)",
            game.game_id,
            draft_view.pick_index,
            DraftView.TOTAL_PICKS,
            f"{remaining:.0f}" if remaining is not None else "full",
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Game(bot))
