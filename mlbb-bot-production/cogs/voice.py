"""
Ког автовозврата в голосовой канал (Этапы 20-21 из ТЗ).

Права доступа к командному каналу уже сохраняются сами по себе (Этап 20)
благодаря тому, что Permission Overwrites выставляются один раз при
создании канала и не снимаются до его удаления — игрок, который
"вылетел" из Discord, всегда может зайти обратно сам, вручную.

Этот ког добавляет НЕОБЯЗАТЕЛЬНОЕ удобство (Этап 21): если игрок,
находившийся в своём командном канале, полностью отключился от голоса
(а не просто демонстративно перешёл в другой канал), бот в течение
короткого "окна прощения" ждёт его возвращения в голос и, если игрок
вернулся именно из состояния "не в голосе вообще", сам перекидывает
его обратно в командный канал.

Защита от бесконечного цикла (важное требование ТЗ, п.21):
  - если игрок НЕ вернулся за время окна — бот прекращает попытки
    и просто забывает про него (никаких повторных проверок);
  - если игрок явно перешёл в ДРУГОЙ голосовой канал, не отключаясь
    полностью (before.channel -> other.channel, both not None), это
    расценивается как осознанный уход, и бот НЕ вмешивается;
  - если игрок вернулся сразу в свой же командный канал, бот тоже
    ничего не делает — он и так там, где нужно.

Fix (round 2, #2 — критический): раньше ЛЮБОЕ появление игрока в
ЛЮБОМ голосовом канале в течение окна прощения (после отключения)
принудительно перемещало его в командный канал — включая случай,
когда игрок специально вышел из командного канала, а через 20 секунд
осознанно зашёл в СОВСЕМ ДРУГОЙ канал. Раньше это работало через
"отмена задачи-таймера триггерит перемещение в except CancelledError",
и решение о перемещении не зависело от того, ПОЧЕМУ задача была
отменена. Теперь два принципиально разных события обрабатываются
раздельно:
  1. Прямой переход между двумя каналами (before и after оба заданы) —
     всегда осознанный выбор, автовозврат НЕ применяется, и если
     почему-то было открыто окно ожидания — оно просто тихо
     закрывается, БЕЗ перемещения.
  2. Появление в голосе ПОСЛЕ полного отключения (before.channel is
     None, то есть до этого события игрок физически не был ни в
     одном голосовом канале) — вот это и есть "вернулся" в смысле
     ТЗ, и только в этом случае вызывается автоперемещение обратно
     в командный канал.
"""

import asyncio
import logging

import discord
from discord.ext import commands

log = logging.getLogger("mlbb-bot.voice")

GRACE_PERIOD_SECONDS = 60  # окно, в течение которого сработает автовозврат


class VoiceRecovery(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # (guild_id, member_id) -> asyncio.Task таймера ожидания
        self._pending: dict[tuple[int, int], asyncio.Task] = {}
        # (guild_id, member_id) -> (team_channel_id, game_id) — контекст,
        # ради которого таймер был открыт; хранится отдельно от самой
        # задачи, чтобы код обработки реконнекта не зависел от механизма
        # отмены/исключений внутри таймера.
        self._pending_context: dict[tuple[int, int], tuple[int, int]] = {}

    async def _find_team_channel(self, guild_id: int, channel_id: int):
        """Если channel_id — это командный голосовой канал активной игры
        (PLAYING или ещё не удалённый PENDING_CONFIRMATION), возвращает
        (game_id, team_channel_id). Иначе — (None, None)."""
        game = await self.bot.db.get_active_game(guild_id)
        if game is None or game.status not in ("PLAYING", "PENDING_CONFIRMATION"):
            return None, None
        if channel_id == game.blue_voice_channel_id or channel_id == game.red_voice_channel_id:
            return game.game_id, channel_id
        return None, None

    def _cancel_pending(self, key: tuple[int, int]):
        task = self._pending.pop(key, None)
        self._pending_context.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        if member.bot:
            return

        key = (member.guild.id, member.id)

        # --- Случай 1: прямой переход между двумя каналами ------------------
        # before и after оба заданы -> игрок не был "полностью вне голоса"
        # ни секунды, это осознанный, добровольный переход. Автовозврат
        # никогда не должен вмешиваться сюда (Fix round 2, #2).
        if before.channel is not None and after.channel is not None:
            self._cancel_pending(key)
            return

        # --- Случай 2: игрок появился в голосе ПОСЛЕ полного отключения -----
        # before.channel is None означает, что непосредственно перед этим
        # событием игрок не состоял ни в одном голосовом канале — то есть
        # это именно "вернулся" в смысле ТЗ п.21, а не переключился.
        if before.channel is None and after.channel is not None:
            task = self._pending.pop(key, None)
            context = self._pending_context.pop(key, None)
            if task is not None and not task.done():
                task.cancel()
            if context is not None:
                team_channel_id, game_id = context
                await self._try_move_back(member, team_channel_id, game_id)
            return

        # --- Случай 3: игрок полностью отключился от голоса -----------------
        if before.channel is not None and after.channel is None:
            game_id, team_channel_id = await self._find_team_channel(member.guild.id, before.channel.id)
            if game_id is None:
                return

            user = await self.bot.db.get_user_by_discord_id(member.id)
            if user is None or not await self.bot.db.is_player_in_game(game_id, user.id):
                return  # отключился кто-то посторонний (например, зашедший админ)

            if key in self._pending:
                return  # окно уже открыто, не дублируем

            self._pending_context[key] = (team_channel_id, game_id)
            self._pending[key] = asyncio.create_task(self._timeout_forget(key, game_id))
            log.info(
                "Игрок %s отключился от командного канала %s (игра #%s) — открыто окно автовозврата на %ss",
                member.id, team_channel_id, game_id, GRACE_PERIOD_SECONDS,
            )

    async def _timeout_forget(self, key: tuple[int, int], game_id: int):
        """Просто ждёт истечения окна прощения. Если игрок за это время
        появится в голосе (случай 1 или 2 выше) — обработчик выше сам
        снимет задачу из self._pending и, если нужно, вызовет
        _try_move_back НАПРЯМУЮ; эта функция никакой бизнес-логики,
        связанной с перемещением, больше не содержит (Fix round 2, #2) —
        только отсчёт времени и, если оно вышло, "забывание" игрока."""
        try:
            await asyncio.sleep(GRACE_PERIOD_SECONDS)
        except asyncio.CancelledError:
            return  # обработано в on_voice_state_update — здесь делать нечего

        self._pending.pop(key, None)
        self._pending_context.pop(key, None)
        log.info(
            "Игрок %s не вернулся за %ss — автовозврат для игры #%s отменён (без повторных попыток)",
            key[1], GRACE_PERIOD_SECONDS, game_id,
        )

    async def _try_move_back(self, member: discord.Member, team_channel_id: int, game_id: int):
        game = await self.bot.db.get_game(game_id)
        if game is None or game.status not in ("PLAYING", "PENDING_CONFIRMATION"):
            return  # игра уже завершилась/отменилась, каналы могли исчезнуть

        current_voice = member.voice
        if current_voice is None or current_voice.channel is None:
            return
        if current_voice.channel.id == team_channel_id:
            return  # уже там, куда нужно — ничего делать не надо

        team_channel = member.guild.get_channel(team_channel_id)
        if team_channel is None:
            return

        try:
            await member.move_to(team_channel, reason=f"MLBB game #{game_id}: автовозврат после отключения")
            log.info("Игрок %s автоматически возвращён в %s (игра #%s)", member.id, team_channel.name, game_id)
        except discord.Forbidden:
            log.warning("Нет прав вернуть %s в командный канал (игра #%s)", member.id, game_id)
        except discord.HTTPException:
            log.exception("Не удалось вернуть %s в командный канал (игра #%s)", member.id, game_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceRecovery(bot))
