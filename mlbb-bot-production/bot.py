"""
MLBB-BOT — точка входа.

Подключает discord.py, БД, все коги и запускает ежедневный бэкап-таск
(Этап 38). Сами команды и игровая логика живут в cogs/ — здесь только
инфраструктура запуска.
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from health import start_health_server
from logging_config import configure_logging
from database.database import Database
from utils.backup import perform_backup

# --- Логирование -----------------------------------------------------------
configure_logging()
log = logging.getLogger("mlbb-bot")


# --- Intents -----------------------------------------------------------------
# members нужен, чтобы видеть участников голосовых каналов и их роли
# voice_states нужен для перемещения игроков и отслеживания голосовых каналов
# message_content нужен для on_message: Discord иначе не передаёт вложения
# пользовательских сообщений, а результат игры приходит скриншотом.
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True


class MLBBBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",  # используется только как запасной вариант, основные команды — slash
            intents=intents,
            help_command=None,
        )
        # Доступна во всех когах как self.bot.db
        self.db = Database(config.DATABASE_PATH)
        self._recovered = False
        self.ready = asyncio.Event()
        self.health_server = None
        self.tree.on_error = self.on_app_command_error

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Единая точка для ошибок slash-команд.

        Пользователь всегда получает понятный ответ вместо стандартного
        "The application did not respond", а полный traceback остаётся в
        логах для диагностики.
        """
        log.error(
            "Ошибка slash-команды %s от пользователя %s",
            getattr(interaction.command, "qualified_name", "unknown"),
            interaction.user.id,
            exc_info=(type(error), error, error.__traceback__),
        )
        if isinstance(error, app_commands.CheckFailure):
            text = "❌ У вас нет прав на выполнение этой команды."
        else:
            text = "❌ Во время выполнения команды произошла ошибка. Администратор уже видит её в логах."

        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def setup_hook(self) -> None:
        """Вызывается один раз при старте, до подключения к Discord Gateway."""
        await self.db.connect()

        for extension in config.INITIAL_EXTENSIONS:
            try:
                await self.load_extension(extension)
                log.info("Загружен ког: %s", extension)
            except Exception:
                log.exception("Не удалось загрузить ког: %s", extension)
                # Не запускаем частично работающего бота: отсутствующий ког
                # может означать, что команды/проверки безопасности не загружены.
                raise

        # Синхронизация slash-команд.
        # На этапе разработки удобнее синкать в конкретную гильдию (мгновенно),
        # а не глобально (может занимать до часа).
        if config.DEV_GUILD_ID:
            guild = discord.Object(id=config.DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Синхронизировано %d команд в гильдию %s", len(synced), config.DEV_GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info("Синхронизировано %d команд глобально", len(synced))

    async def on_ready(self):
        log.info("Бот запущен как %s (ID: %s)", self.user, self.user.id)
        self.ready.set()
        if self.health_server is None:
            self.health_server = await start_health_server(config.HEALTH_HOST, config.HEALTH_PORT, self.ready)
        log.info("Активен на %d сервере(ах)", len(self.guilds))
        if not self.daily_backup.is_running():
            self.daily_backup.start()

        # on_ready может сработать повторно при переподключении к Gateway —
        # восстановление игр должно происходить только один раз за запуск процесса.
        if not self._recovered:
            self._recovered = True
            await self._recover_active_games()

    async def _recover_active_games(self):
        """Этап 39: восстановление игр, которые были активны на момент
        предыдущей остановки/перезапуска бота."""
        game_cog = self.get_cog("Game")
        results_cog = self.get_cog("Results")

        if game_cog is not None:
            try:
                await game_cog.recover_games()
            except Exception:
                log.exception("Ошибка при восстановлении WAITING/DRAFT игр")

        if results_cog is not None:
            try:
                await results_cog.recover_pending_confirmations()
            except Exception:
                log.exception("Ошибка при восстановлении PENDING_CONFIRMATION игр")

            # Fix #7: удаляем голосовые каналы, которые остались от игр,
            # завершившихся до предыдущей остановки бота (отложенное
            # удаление через asyncio.create_task не переживает рестарт).
            try:
                await results_cog.cleanup_stale_voice_channels()
            except Exception:
                log.exception("Ошибка при очистке оставшихся голосовых каналов после рестарта")

    @tasks.loop(hours=24)
    async def daily_backup(self):
        try:
            await perform_backup(self.db)
        except Exception:
            log.exception("Ошибка при создании автоматического бэкапа")

    async def close(self):
        self.ready.clear()
        if self.health_server is not None:
            self.health_server.close()
            await self.health_server.wait_closed()
            self.health_server = None
        self.daily_backup.cancel()
        await self.db.close()
        await super().close()

    async def on_error(self, event_method: str, *args, **kwargs):
        """Last-resort Gateway/event error logger; never leak secrets."""
        log.exception("Необработанная ошибка Discord event=%s", event_method)


async def main():
    bot = MLBBBot()
    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
