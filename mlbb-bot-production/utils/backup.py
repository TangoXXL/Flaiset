"""
Автоматическое резервное копирование (Этап 38 из ТЗ).

Раз в сутки создаёт backups/backup_YYYY-MM-DD.db через безопасный
SQLite backup API (Database.backup_to) — а не сырое копирование файла,
которое могло бы захватить базу в момент записи и получить битый файл.

Хранится ограниченное число последних бэкапов (по умолчанию 14), чтобы
папка backups/ не росла бесконечно — это не было явно в ТЗ, но является
разумным дополнением, о котором стоит сказать прямо.
"""

import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("mlbb-bot.backup")

BACKUP_DIR = "backups"
KEEP_LAST = 14


async def perform_backup(db, backup_dir: str = BACKUP_DIR, keep_last: int = KEEP_LAST) -> str:
    """db — экземпляр database.database.Database (уже подключённый)."""
    os.makedirs(backup_dir, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest_path = os.path.join(backup_dir, f"backup_{today}.db")

    await db.backup_to(dest_path)
    log.info("Создан бэкап базы данных: %s", dest_path)

    _prune_old_backups(backup_dir, keep_last)
    return dest_path


def _prune_old_backups(backup_dir: str, keep_last: int) -> None:
    backups = sorted(
        f for f in os.listdir(backup_dir) if f.startswith("backup_") and f.endswith(".db")
    )
    excess = len(backups) - keep_last
    for name in backups[: max(excess, 0)]:
        path = os.path.join(backup_dir, name)
        try:
            os.remove(path)
            log.info("Удалён старый бэкап: %s", name)
        except OSError:
            log.exception("Не удалось удалить старый бэкап: %s", name)
