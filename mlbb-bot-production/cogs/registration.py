"""
Ког регистрации игроков.

/register открывает Discord Modal (всплывающую форму) с двумя полями —
MLBB Player ID и MLBB Server ID — как описано в актуальном ТЗ. Мы
используем именно Modal, а не обычный обмен сообщениями, потому что это
официальный Discord UI для сбора текстового ввода и не тонет в истории
чата.

Регистрация НЕ использует MLBB API (актуальное ТЗ, п.1/п.24): никакой
автоматической проверки Player ID, получения ника или подтверждения
аккаунта нет и не планируется на этом этапе. Мы просто валидируем формат
введённых значений (только цифры, строгая длина) и сохраняем их в БД.
services/mlbb_api.py — пустой модуль на будущее, регистрация от него
не зависит.

Формат полей (актуальное ТЗ, п.1):
  - MLBB Player ID — только цифры, максимум 11 цифр, без букв/пробелов/символов;
  - MLBB Server ID — только цифры, максимум 5 цифр, без букв/пробелов/символов.

Важно: ввод НЕ обрезается (.strip()) перед проверкой формата — пробелы
в начале/конце (и любые другие символы) должны приводить к отказу, а не
незаметно отбрасываться. Раз оба значения по построению состоят только
из цифр после успешной проверки регулярным выражением, дополнительная
очистка перед сохранением в БД не нужна.
"""

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from utils.rank import get_rank

log = logging.getLogger("mlbb-bot.registration")

# Строгий формат по актуальному ТЗ: только цифры, ограничение по длине.
PLAYER_ID_RE = re.compile(r"^[0-9]{1,11}$")
SERVER_ID_RE = re.compile(r"^[0-9]{1,5}$")


class RegisterModal(discord.ui.Modal, title="Регистрация Mobile Legends"):
    mlbb_id = discord.ui.TextInput(
        label="MLBB Player ID",
        placeholder="Максимум 11 цифр, например: 12345678901",
        required=True,
        min_length=1,
        max_length=20,
    )
    server_id = discord.ui.TextInput(
        label="MLBB Server ID",
        placeholder="До 5 цифр, например: 12345",
        required=True,
        min_length=1,
        max_length=20,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        db = self.bot.db
        # ВАЖНО: значения берутся как есть, БЕЗ .strip(). Требование ТЗ —
        # только цифры 0-9, поэтому пробелы (в том числе в начале/конце)
        # должны приводить к отказу в регистрации, а не тихо отбрасываться.
        mlbb_id = str(self.mlbb_id.value)
        server_id = str(self.server_id.value)

        # Валидация формата (актуальное ТЗ, п.1). Делаем это ДО любых
        # обращений к БД — некорректный ввод не должен создавать/менять
        # запись пользователя. max_length у TextInput намеренно оставлен
        # шире (20), чтобы можно было явно объяснить причину отказа
        # (слишком длинно / есть буквы/пробелы), а не тихо обрезать ввод.
        if not PLAYER_ID_RE.fullmatch(mlbb_id):
            if not mlbb_id or not mlbb_id.isascii() or not mlbb_id.isdigit():
                reason = "должен содержать только цифры (0-9), без букв, пробелов и символов"
            else:
                reason = f"максимум 11 цифр (введено: {len(mlbb_id)})"
            await interaction.response.send_message(
                f"❌ MLBB Player ID указан неверно: {reason}.\nПример корректного значения: `12345678901`.",
                ephemeral=True,
            )
            return

        if not SERVER_ID_RE.fullmatch(server_id):
            if not server_id or not server_id.isascii() or not server_id.isdigit():
                reason = "должен содержать только цифры (0-9), без букв, пробелов и символов"
            else:
                reason = f"максимум 5 цифр (введено: {len(server_id)})"
            await interaction.response.send_message(
                f"❌ MLBB Server ID указан неверно: {reason}.\nПример корректного значения: `12345`.",
                ephemeral=True,
            )
            return

        existing = await db.get_user_by_discord_id(interaction.user.id)

        # Fix #8: один MLBB-аккаунт (MLBB ID + Server ID) не может быть
        # привязан сразу к двум разным Discord-аккаунтам. Проверка на
        # уровне приложения — основная защита; уникальный индекс в БД
        # (см. database.py, _migrate) подстраховывает на случай гонки.
        mlbb_owner = await db.get_user_by_mlbb(mlbb_id, server_id)
        if mlbb_owner is not None and (existing is None or mlbb_owner.id != existing.id):
            await interaction.response.send_message(
                "❌ Этот MLBB ID уже привязан к другому Discord-аккаунту на этом сервере. "
                "Если это ошибка, обратитесь к администратору.",
                ephemeral=True,
            )
            log.warning(
                "Попытка регистрации с уже занятым MLBB ID=%s server=%s: discord_id=%s, владелец discord_id=%s",
                mlbb_id, server_id, interaction.user.id, mlbb_owner.discord_id,
            )
            return

        if existing is not None:
            # Уже зарегистрирован — обновляем MLBB ID, а не создаём дубликат
            # и не сбрасываем ELO/статистику.
            #
            # Fix (round 3, критический — требование п.5 ревью): используем
            # атомарную версию, которая ловит IntegrityError вместо того,
            # чтобы уронить обработчик интеракции. Гонка возможна, если
            # ровно в момент между проверкой mlbb_owner выше и этим UPDATE
            # кто-то другой успел занять тот же MLBB ID.
            updated = await db.update_mlbb_info_atomic(interaction.user.id, mlbb_id, server_id)
            if not updated:
                await interaction.response.send_message(
                    "❌ Этот MLBB ID только что был занят другим Discord-аккаунтом — "
                    "попробуйте зарегистрироваться ещё раз или обратитесь к администратору.",
                    ephemeral=True,
                )
                log.warning(
                    "Гонка при обновлении MLBB ID=%s server=%s: discord_id=%s",
                    mlbb_id, server_id, interaction.user.id,
                )
                return

            embed = discord.Embed(
                title="🔄 Данные обновлены",
                description=(
                    f"🎮 MLBB Player ID: **{mlbb_id}**\n"
                    f"🌐 Server ID: **{server_id}**\n\n"
                    "Твой ELO и статистика сохранены без изменений."
                ),
                color=discord.Color.blurple(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            log.info("Игрок %s обновил MLBB ID", interaction.user.id)
            return

        # Fix (round 3, критический — требование п.5 ревью): create_user_atomic
        # ловит IntegrityError вместо того, чтобы уронить обработчик
        # интеракции необработанным исключением ("Interaction failed" без
        # понятной причины). Гонка возможна при двойном сабмите формы
        # (двойной клик, ретрай интеракции) — оба прохода могли увидеть
        # existing=None до того, как первый закоммитил свою запись.
        user = await db.create_user_atomic(
            discord_id=interaction.user.id,
            discord_username=str(interaction.user),
            mlbb_id=mlbb_id,
            server_id=server_id,
        )

        if user is None:
            # Кто-то опередил нас между проверками выше и INSERT — выясняем,
            # что именно произошло, чтобы дать понятный ответ вместо
            # generic ошибки.
            existing_after = await db.get_user_by_discord_id(interaction.user.id)
            if existing_after is not None:
                await interaction.response.send_message(
                    "⚠️ Похоже, вы уже зарегистрированы (регистрация была отправлена дважды). "
                    "Используйте `/register` ещё раз, если нужно изменить MLBB ID.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "❌ Этот MLBB ID только что был занят другим Discord-аккаунтом — "
                    "попробуйте зарегистрироваться ещё раз или обратитесь к администратору.",
                    ephemeral=True,
                )
            log.warning(
                "Гонка при регистрации MLBB ID=%s server=%s: discord_id=%s",
                mlbb_id, server_id, interaction.user.id,
            )
            return

        tier = get_rank(user.elo)
        embed = discord.Embed(
            title="🎮 Регистрация Mobile Legends",
            description=(
                f"✅ Ты успешно зарегистрирован!\n\n"
                f"🎮 MLBB Player ID: **{user.mlbb_id}**\n"
                f"🌐 Server ID: **{user.server_id}**\n"
                f"⭐ Стартовый ELO: **{user.elo}** ({tier.name})\n\n"
                f"Посмотреть карточку профиля можно командой `/profile`."
            ),
            color=discord.Color.from_rgb(*tier.color),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class Registration(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="register", description="Зарегистрироваться как игрок Mobile Legends")
    async def register(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RegisterModal(self.bot))


async def setup(bot: commands.Bot):
    await bot.add_cog(Registration(bot))
