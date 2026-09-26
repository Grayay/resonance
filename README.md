# Telegram-бот «Резонанс»

Production-ready MVP для регистрации на мероприятия сообщества. Бот работает на Python 3.12, aiogram 3, PostgreSQL и SQLAlchemy 2 (async), управляется полностью через Telegram и публикует анонсы в канал.

## Что реализовано

- роли пользователя, администратора и суперадминистратора;
- добавление и удаление администраторов внутри бота;
- пошаговое создание мероприятия с предпросмотром, редактированием, черновиком и публикацией;
- публикация фото и HTML-анонса в канал, включая корректный fallback для длинной подписи;
- deep link на конкретное мероприятие и готовый текст для ручной публикации;
- регистрация без анкеты, защита от дублей и переполнения;
- FIFO-лист ожидания с резервированием места, ограниченным по времени предложением и автоматическим переходом к следующему участнику;
- список ближайших событий и раздел «Мои записи»;
- подтверждение отмены записи;
- устойчивые к перезапуску напоминания и запросы отзыва через таблицу заданий в PostgreSQL;
- оценки 1–5, комментарий и ответ «не получилось прийти»;
- просмотр участников и статусов, статистика, CSV-экспорт с BOM для Excel;
- рассылка участникам выбранного события и общая рассылка;
- уведомление участников при изменении даты, времени или места;
- уведомление при отмене мероприятия;
- Alembic, Docker Compose, логирование, graceful shutdown и pytest-тесты бизнес-логики.

Транзакции регистрации блокируют строку мероприятия (`SELECT ... FOR UPDATE`) до проверки вместимости и записи результата. Предложение листа ожидания временно резервирует освободившееся место. Это предотвращает продажу последнего места двум пользователям при нескольких экземплярах приложения. SQLite используется только в быстрых unit-тестах; боевой режим рассчитан на PostgreSQL.

## Структура

```text
resonance_bot/
  handlers/          Telegram-команды, callback-и и FSM
  db/                модели и подключение SQLAlchemy
  services/          регистрация, публикация, рассылки, экспорт
  utils/             даты и форматирование
  scheduler.py       постоянный DB-backed worker
  config.py          настройки из .env
  main.py            запуск и graceful shutdown
alembic/              миграции
tests/                тесты основных бизнес-правил
```

## Настройка Telegram

1. Откройте [@BotFather](https://t.me/BotFather), выполните `/newbot`, задайте имя и username.
2. Скопируйте токен бота — он понадобится как `BOT_TOKEN`.
3. Узнайте свой числовой Telegram ID, например через [@userinfobot](https://t.me/userinfobot), и укажите его как `SUPERADMIN_ID`.
4. Добавьте бота администратором канала. Ему нужны права публикации сообщений и редактирования своих сообщений.
5. Узнайте `CHANNEL_ID`:
   - у публичного канала можно использовать `@channel_username`;
   - для приватного канала нужен числовой ID вида `-100...` (его можно получить через Bot API `getUpdates` после пересылки/публикации тестового сообщения или с помощью служебного бота).
6. Укажите username бота без `@` в `BOT_USERNAME`.
7. Укажите контакт сообщества в `COMMUNITY_CONTACT`: `https://t.me/name` или `@name`.

Кнопка «Поделиться» реализована стандартной Telegram share-ссылкой. Telegram не позволяет боту автоматически открыть системное окно выбора контакта через callback, поэтому пользователь переходит в нативный экран шаринга по URL.

## Запуск через Docker Compose

Требования: Docker Engine с Compose v2.

```bash
cp .env.example .env
```

Заполните `.env`. Для Compose оставьте адрес базы с хостом `db`:

```dotenv
BOT_TOKEN=123456:replace-me
SUPERADMIN_ID=123456789
DATABASE_URL=postgresql+asyncpg://resonance:change-me@db:5432/resonance
CHANNEL_ID=@my_channel
COMMUNITY_CONTACT=https://t.me/my_contact
TIMEZONE=Europe/Moscow
BOT_USERNAME=my_resonance_bot
WAITLIST_OFFER_TTL_MINUTES=120
SCHEDULER_POLL_SECONDS=20
DEFAULT_REMINDER_HOURS=24,3
LOG_LEVEL=INFO
POSTGRES_PASSWORD=change-me
```

Пароль в `DATABASE_URL` должен совпадать с `POSTGRES_PASSWORD`.

Соберите и запустите:

```bash
docker compose up --build -d
docker compose logs -f bot
```

Сервис `migrate` дождётся PostgreSQL и выполнит `alembic upgrade head`; бот стартует только после успешной миграции.

Остановка:

```bash
docker compose down
```

Данные PostgreSQL остаются в volume `postgres_data`. Команда `docker compose down -v` удалит и volume — используйте её только если данные действительно больше не нужны.

## Локальный запуск без Docker

Нужны Python 3.12+ и PostgreSQL 15+.

```bash
python -m venv .venv
```

Linux/macOS:

```bash
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

PowerShell:

```powershell
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
```

В локальном `.env` укажите реальный адрес PostgreSQL, например:

```dotenv
DATABASE_URL=postgresql+asyncpg://resonance:change-me@localhost:5432/resonance
```

Примените миграции и запустите приложение:

```bash
alembic upgrade head
python -m resonance_bot
```

При первом запуске суперадминистратор должен открыть бота командой `/start`. Панель администратора открывается командой `/admin`.

## Ubuntu VPS deployment without Docker

Ниже приведён вариант установки на Ubuntu 24.04 LTS без Docker. Эта версия Ubuntu содержит требуемый Python 3.12. Приложение запускается существующей командой `python -m resonance_bot`, читает конфигурацию из `.env` и устанавливается из существующего `pyproject.toml`.

### 1. Системные пакеты и PostgreSQL

```bash
sudo apt update
sudo apt install -y \
  python3.12 python3.12-venv python3-pip \
  postgresql postgresql-contrib \
  build-essential libpq-dev ca-certificates git

sudo systemctl enable --now postgresql
sudo systemctl status postgresql --no-pager
```

Создайте роль и базу данных. Перед выполнением замените `CHANGE_ME_STRONG_PASSWORD` на надёжный пароль, состоящий из букв и цифр. Если пароль содержит символы `@`, `:`, `/`, `?`, `#` или `%`, его необходимо URL-кодировать в `DATABASE_URL`.

```bash
sudo -u postgres psql -v ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE resonance LOGIN PASSWORD 'CHANGE_ME_STRONG_PASSWORD';
CREATE DATABASE resonance OWNER resonance ENCODING 'UTF8';
SQL
```

Проверьте подключение:

```bash
PGPASSWORD='CHANGE_ME_STRONG_PASSWORD' \
  psql -h 127.0.0.1 -U resonance -d resonance -c 'SELECT 1;'
```

### 2. Системный пользователь и файлы приложения

Создайте отдельного непривилегированного пользователя и каталог приложения:

```bash
sudo adduser --system --group --home /opt/resonance-bot resonance
sudo install -d -o resonance -g resonance /opt/resonance-bot
```

Загрузите или клонируйте проект в `/opt/resonance-bot`. Например, для Git-репозитория замените `REPOSITORY_URL` на фактический адрес:

```bash
sudo -u resonance git clone REPOSITORY_URL /opt/resonance-bot
```

Если каталог уже создан и Git отказывается клонировать в него, загрузите файлы проекта другим способом, затем установите владельца:

```bash
sudo chown -R resonance:resonance /opt/resonance-bot
```

В каталоге должны находиться `pyproject.toml`, `alembic.ini`, `alembic/`, `resonance_bot/` и `deploy/resonance-bot.service`.

### 3. Виртуальное окружение и зависимости

Проект использует `pyproject.toml`; `requirements.txt` не требуется.

```bash
sudo -u resonance python3.12 -m venv /opt/resonance-bot/.venv
sudo -u resonance /opt/resonance-bot/.venv/bin/python -m pip install --upgrade pip
sudo -u resonance /opt/resonance-bot/.venv/bin/python -m pip install /opt/resonance-bot
```

### 4. Конфигурация `.env`

```bash
sudo -u resonance cp /opt/resonance-bot/.env.example /opt/resonance-bot/.env
sudoedit /opt/resonance-bot/.env
sudo chown resonance:resonance /opt/resonance-bot/.env
sudo chmod 600 /opt/resonance-bot/.env
```

Минимальный пример для PostgreSQL на том же VPS:

```dotenv
BOT_TOKEN=123456:replace-me
SUPERADMIN_ID=123456789
DATABASE_URL=postgresql+asyncpg://resonance:CHANGE_ME_STRONG_PASSWORD@127.0.0.1:5432/resonance
CHANNEL_ID=@my_channel
COMMUNITY_CONTACT=https://t.me/my_contact
TIMEZONE=Europe/Moscow
BOT_USERNAME=my_resonance_bot
WAITLIST_OFFER_TTL_MINUTES=120
SCHEDULER_POLL_SECONDS=20
DEFAULT_REMINDER_HOURS=24,3
LOG_LEVEL=INFO
```

Схема URL `postgresql+asyncpg://` обязательна: приложение использует асинхронный драйвер `asyncpg`.

### 5. Миграции

Примените существующие Alembic-миграции от имени сервисного пользователя:

```bash
cd /opt/resonance-bot
sudo -u resonance /opt/resonance-bot/.venv/bin/alembic upgrade head
sudo -u resonance /opt/resonance-bot/.venv/bin/alembic current
```

### 6. Установка systemd-сервиса

Репозиторий содержит готовый unit-файл `deploy/resonance-bot.service`, рассчитанный на каталог `/opt/resonance-bot` и пользователя `resonance`.

```bash
sudo cp /opt/resonance-bot/deploy/resonance-bot.service \
  /etc/systemd/system/resonance-bot.service
sudo chmod 644 /etc/systemd/system/resonance-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now resonance-bot.service
sudo systemctl status resonance-bot.service --no-pager
```

Управление сервисом:

```bash
sudo systemctl start resonance-bot.service
sudo systemctl restart resonance-bot.service
sudo systemctl stop resonance-bot.service
```

Логи:

```bash
sudo journalctl -u resonance-bot.service -f
sudo journalctl -u resonance-bot.service -n 200 --no-pager
sudo journalctl -u resonance-bot.service --since today
```

После обновления кода повторно установите пакет, примените миграции и перезапустите сервис:

```bash
sudo -u resonance /opt/resonance-bot/.venv/bin/python -m pip install /opt/resonance-bot
cd /opt/resonance-bot
sudo -u resonance /opt/resonance-bot/.venv/bin/alembic upgrade head
sudo systemctl restart resonance-bot.service
```

## Проверки

```bash
pytest
ruff check .
python -m compileall resonance_bot tests
alembic upgrade head
alembic check
```

Тесты используют SQLite в памяти для быстрых проверок правил. Блокировки и `SKIP LOCKED` относятся к интеграционному поведению PostgreSQL и применяются в production-коде.

## Напоминания и отказоустойчивость

Отдельный in-process worker опрашивает таблицу `reminders`. Сами задания, попытки и статусы хранятся в PostgreSQL, поэтому перезапуск бота их не теряет. Зависшие задания возвращаются в очередь через 10 минут. Доставка имеет семантику at-least-once: при аварии ровно между отправкой сообщения и фиксацией статуса редкий дубль теоретически возможен, зато напоминание не теряется без следа.

Для горизонтального запуска нескольких экземпляров используются `FOR UPDATE SKIP LOCKED`. Polling Telegram-обновлений при этом должен выполняться только одним экземпляром; дополнительные экземпляры уместнее выделять под worker после разделения процессов.

## Ручной чек-лист развёртывания

1. Создать бота в BotFather и получить токен.
2. Добавить бота в канал администратором с правами публикации и редактирования своих сообщений.
3. Получить и настроить `CHANNEL_ID`.
4. Заполнить `SUPERADMIN_ID`, `BOT_USERNAME`, `COMMUNITY_CONTACT` и остальные значения `.env`.
5. Запустить PostgreSQL и приложение (`docker compose up --build -d` либо локально).
6. Выполнить миграции (`alembic upgrade head`; в Compose это делает сервис `migrate`).
7. Открыть бота через `/start`, затем проверить `/admin` и тестовую публикацию в канал.

## Эксплуатационные замечания

- Секреты не входят в образ и не должны коммититься: `.env` добавлен в `.gitignore`.
- После публикации вместимость намеренно нельзя менять через бота — это ограничение MVP из требований.
- При ручном закрытии регистрации ожидающие предложения отменяются; новые регистрации не принимаются даже при наличии мест.
- Блокировка пользователем или удалённый аккаунт учитываются как ошибка доставки и не прерывают рассылку.
- Если обновить уже опубликованное мероприятие, исходный пост в канале автоматически не переписывается. Бот хранит ID публикаций, убирает кнопку при закрытии/отмене и предлагает отдельно уведомить зарегистрированных участников.
