"""
Общие фикстуры для тестов.

Все тесты работают напрямую со слоем database.database.Database, используя
файловую SQLite-базу во временном каталоге (не ":memory:") — так поведение
максимально приближено к реальной эксплуатации (включая WAL/busy_timeout,
которые не имеют смысла на ":memory:"), но при этом каждый тест получает
полностью изолированную, автоматически удаляемую после теста базу.
"""

import os
import sys
from pathlib import Path

import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# config.py требует DISCORD_TOKEN при импорте (это единственная обязательная
# переменная окружения бота) — в тестах реальный токен не нужен и не должен
# требоваться, поэтому подставляем безобидную заглушку ДО первого импорта
# config где-либо в тестах.
os.environ.setdefault("DISCORD_TOKEN", "test-token-for-pytest")

from database.database import Database  # noqa: E402


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    try:
        yield database
    finally:
        await database.close()
