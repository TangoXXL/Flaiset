# MLBB Discord Bot

Discord-бот для внутренних матчей Mobile Legends: регистрация игроков,
лобби 5×5, драфт капитанами, временные голосовые каналы, подтверждение
результата и ELO.

## Запуск

Нужен Python 3.11+.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python bot.py
```

Заполни `.env` настоящим `DISCORD_TOKEN` и ID каналов/роли с сервера.
`TEAM_VOICE_CATEGORY_ID`, `RESULTS_CHANNEL_ID` и `ADMIN_ROLE_ID` нужны
для полноценной игры; остальные параметры можно оставить пустыми при
первичной проверке.

## Настройки в Discord Developer Portal

В разделе **Bot → Privileged Gateway Intents** включи:

- **Server Members Intent** — бот проверяет роли и участников;
- **Message Content Intent** — без него Discord не передаёт вложения
  сообщений, поэтому бот не сможет принять скриншот результата;
- **Voice States** включается кодом, отдельного privileged-переключателя
  для него нет.

После изменения intents перезапусти бота.

## Безопасность поставки

`.env`, `database.db` и `backups/` содержат секреты или данные конкретного
сервера. Они намеренно не входят в релизный ZIP. Если токен когда-либо
попадал в чужой архив или репозиторий, перевыпусти его в Developer Portal.

## Multi-guild

Бот поддерживает разные настройки для разных Discord-серверов через `GUILD_CONFIG_JSON`.
Старые значения `.env` остаются fallback для серверов без отдельного блока. Пример:

```text
GUILD_CONFIG_JSON={"123456789012345678":{"results_channel_id":234567890123456789,"admin_role_id":345678901234567890,"players_per_game":10}}
```

Поддерживаются `required_voice_channel_id`, `team_voice_category_id`,
`results_channel_id`, `admin_role_id` и `players_per_game`. `players_per_game`
должен быть чётным числом >= 2.

## Production

Для запуска в production доступны:

- `Dockerfile` + `docker-compose.yml`;
- Docker `HEALTHCHECK` через `GET /health` на порту `8080`;
- ротация логов в `logs/mlbb-bot.log`;
- GitHub Actions: Ruff + pytest на Python 3.12;
- контейнер запускается от непривилегированного пользователя;
- `.env`, SQLite и runtime-директории исключены из Docker context.

Для Kubernetes/VM health endpoint можно использовать как liveness/readiness probe.
