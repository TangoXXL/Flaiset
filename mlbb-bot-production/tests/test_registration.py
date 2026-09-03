"""
Тесты регистрации: обычная регистрация, дубликаты, гонки, а также
валидация формата MLBB Player ID / Server ID (актуальное ТЗ, п.1).

Валидация формата тестируется через РЕАЛЬНЫЙ RegisterModal.on_submit
(а не только через регулярки напрямую) — Discord Interaction подменяется
лёгкими фейками (FakeInteraction/FakeUser/FakeResponse), а БД — настоящий
файловый Database из фикстуры `db`, так что тест проверяет ровно то, что
произойдёт в проде: либо запись отклоняется и пользователь не создаётся,
либо создаётся с ровно тем значением, что было введено.
"""

import asyncio

import pytest

from cogs.registration import RegisterModal


# --- Фейки Discord Interaction, достаточные для RegisterModal.on_submit ---

class FakeResponse:
    def __init__(self):
        self.messages: list[dict] = []

    async def send_message(self, content=None, *, embed=None, ephemeral=False):
        self.messages.append({"content": content, "embed": embed, "ephemeral": ephemeral})

    @property
    def sent(self) -> bool:
        return bool(self.messages)

    @property
    def last(self) -> dict:
        return self.messages[-1]


class FakeUser:
    def __init__(self, user_id: int, name: str = "Tester"):
        self.id = user_id
        self._name = name

    def __str__(self) -> str:
        return self._name


class FakeInteraction:
    def __init__(self, user_id: int, name: str = "Tester"):
        self.user = FakeUser(user_id, name)
        self.response = FakeResponse()


class FakeBot:
    def __init__(self, db):
        self.db = db


def _make_modal(bot, player_id: str, server_id: str) -> RegisterModal:
    """Создаёт RegisterModal и подставляет введённые значения напрямую в
    приватное поле TextInput._value — то же самое, что Discord делает при
    сабмите формы (публичного сеттера у TextInput.value нет)."""
    modal = RegisterModal(bot)
    modal.mlbb_id._value = player_id
    modal.server_id._value = server_id
    return modal


# --- Отклонение некорректного формата (актуальное ТЗ, п.1) ----------------

@pytest.mark.parametrize(
    "player_id,server_id",
    [
        ("1234567890123", "12345"),  # Player ID: 13 цифр — превышает максимум 11
        ("12345abc901", "12345"),    # Player ID: буквы внутри
        ("1234567890a", "12345"),    # Player ID: буква на конце
        ("123 4567890", "12345"),    # Player ID: пробел внутри
        (" 12345678901", "12345"),   # Player ID: пробел в начале — НЕ должен быть отброшен (нет .strip())
        ("12345678901 ", "12345"),   # Player ID: пробел в конце — то же самое
        ("", "12345"),                # Player ID: пусто
        ("12345678901", "123456"),   # Server ID: 6 цифр — превышает максимум 5
        ("12345678901", "12a45"),    # Server ID: буквы внутри
        ("12345678901", " 12345"),   # Server ID: пробел в начале
        ("12345678901", "12345 "),   # Server ID: пробел в конце
        ("12345678901", ""),          # Server ID: пусто
    ],
)
async def test_on_submit_rejects_invalid_format(db, player_id, server_id):
    bot = FakeBot(db)
    modal = _make_modal(bot, player_id, server_id)
    interaction = FakeInteraction(user_id=1)

    await modal.on_submit(interaction)

    assert interaction.response.sent
    assert interaction.response.last["ephemeral"] is True
    assert "❌" in interaction.response.last["content"]
    # Некорректный ввод не должен создавать запись в БД.
    assert await db.get_user_by_discord_id(1) is None


# --- Пограничные корректные значения ---------------------------------------

@pytest.mark.parametrize(
    "player_id,server_id",
    [
        ("1", "1"),                    # минимально короткие, но валидные значения
        ("12345678901", "12345"),      # максимальная длина обоих полей (11 и 5 цифр)
    ],
)
async def test_on_submit_accepts_valid_format(db, player_id, server_id):
    bot = FakeBot(db)
    modal = _make_modal(bot, player_id, server_id)
    interaction = FakeInteraction(user_id=1)

    await modal.on_submit(interaction)

    assert interaction.response.sent
    # Успешная регистрация отвечает embed'ом, а не текстом с "❌".
    assert interaction.response.last["embed"] is not None

    user = await db.get_user_by_discord_id(1)
    assert user is not None
    # Значение сохраняется ровно как введено — без побочной обрезки пробелов
    # (которых тут нет, но сама логика не должна модифицировать ввод).
    assert user.mlbb_id == player_id
    assert user.server_id == server_id


async def test_on_submit_does_not_strip_valid_looking_but_padded_input(db):
    """Ключевой тест на требование "без .strip()": строка, которая стала бы
    валидной ПОСЛЕ обрезки пробелов, обязана быть отклонена как есть."""
    bot = FakeBot(db)
    modal = _make_modal(bot, " 12345678901 ", "12345")
    interaction = FakeInteraction(user_id=1)

    await modal.on_submit(interaction)

    assert "❌" in interaction.response.last["content"]
    assert await db.get_user_by_discord_id(1) is None


async def test_register_new_user(db):
    user = await db.create_user_atomic(
        discord_id=1, discord_username="Alice", mlbb_id="111", server_id="1"
    )
    assert user is not None
    assert user.discord_id == 1
    assert user.mlbb_id == "111"
    assert user.elo == 100  # STARTING_ELO по умолчанию


async def test_duplicate_discord_id_rejected(db):
    """Один и тот же discord_id не может быть зарегистрирован дважды."""
    first = await db.create_user_atomic(1, "Alice", "111", "1")
    assert first is not None

    second = await db.create_user_atomic(1, "Alice", "222", "2")
    assert second is None  # INSERT должен упасть на UNIQUE(discord_id), а не крашнуть тест

    # В базе всё ещё ровно один пользователь с исходными данными.
    stored = await db.get_user_by_discord_id(1)
    assert stored is not None
    assert stored.mlbb_id == "111"


async def test_duplicate_mlbb_account_rejected(db):
    """Один MLBB ID + Server ID не может принадлежать двум разным discord-аккаунтам."""
    owner = await db.create_user_atomic(1, "Alice", "111", "1")
    assert owner is not None

    other = await db.create_user_atomic(2, "Bob", "111", "1")
    assert other is None

    found = await db.get_user_by_mlbb("111", "1")
    assert found is not None
    assert found.discord_id == 1


async def test_update_mlbb_info_preserves_stats(db):
    user = await db.create_user_atomic(1, "Alice", "111", "1")
    game = await db.create_game(guild_id=123)
    await db.apply_game_result(game.game_id, user.id, 25, "WIN")

    await db.update_mlbb_info_atomic(1, "999", "9")

    updated = await db.get_user_by_discord_id(1)
    assert updated.mlbb_id == "999"
    assert updated.server_id == "9"
    assert updated.elo == 125  # статистика/ELO не сброшены при обновлении MLBB ID
    assert updated.wins == 1


async def test_update_mlbb_info_race_rejected(db):
    """Обновление на уже занятый другим игроком MLBB ID не должно проходить
    и не должно ронять вызывающий код исключением."""
    await db.create_user_atomic(1, "Alice", "111", "1")
    await db.create_user_atomic(2, "Bob", "222", "2")

    ok = await db.update_mlbb_info_atomic(2, "111", "1")  # Bob пытается занять MLBB Alice
    assert ok is False

    bob = await db.get_user_by_discord_id(2)
    assert bob.mlbb_id == "222"  # данные Bob не изменились


async def test_concurrent_registration_same_discord_id(db):
    """Двойной сабмит формы /register одним и тем же пользователем
    (например, повторная интеракция) не должен создать два аккаунта и не
    должен приводить к необработанному исключению — ровно одна попытка
    должна успеть, вторая — вернуть None."""
    results = await asyncio.gather(
        db.create_user_atomic(1, "Alice", "111", "1"),
        db.create_user_atomic(1, "Alice", "222", "2"),
    )
    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1

    all_users = await db.get_leaderboard(limit=100)
    assert len(all_users) == 1


async def test_concurrent_registration_same_mlbb_id(db):
    """Два РАЗНЫХ discord-аккаунта одновременно пытаются занять один и тот
    же MLBB ID — должен победить только один."""
    results = await asyncio.gather(
        db.create_user_atomic(1, "Alice", "555", "5"),
        db.create_user_atomic(2, "Bob", "555", "5"),
    )
    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1

    all_users = await db.get_leaderboard(limit=100)
    assert len(all_users) == 1
