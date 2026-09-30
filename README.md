# tg-bot

Telegram-бот для генерации студенческих документов. Первый коммерческий сценарий —
**отчёт по практике в DOCX**.

Текущее состояние: каркас проекта. Есть регистрация пользователей, главное меню,
схема БД и миграции. Генерация документов, LLM, платежи и очередь задач пока не
реализованы — для них заложены интерфейсы.

## Требования

- Python 3.12+
- Docker Desktop (для PostgreSQL и Redis)
- Токен бота от [@BotFather](https://t.me/BotFather)

## Локальный запуск

PowerShell (Windows):

```powershell
# 1. Виртуальное окружение и зависимости
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 2. Настройки: скопируйте пример и впишите BOT_TOKEN
Copy-Item .env.example .env

# 3. PostgreSQL и Redis
docker compose up -d

# 4. Миграции
alembic upgrade head

# 5. Бот
python -m app.main_bot
```

В bash/zsh отличаются только активация окружения (`source .venv/bin/activate`)
и копирование файла (`cp .env.example .env`).

## Тесты и линтер

```powershell
docker compose up -d      # тестам нужен PostgreSQL
pytest
ruff check .
ruff format --check .
```

Тесты работают на отдельной БД `tg_bot_test` (создаётся `docker/postgres/init.sql`
при первом запуске контейнера). Перед прогоном схема пересоздаётся миграциями
(`downgrade base` → `upgrade head`), каждый тест выполняется в транзакции, которая
откатывается. `tests/test_migrations.py` проверяет, что модели и миграции не расходятся.

> Если том `postgres_data` был создан до появления `init.sql`, тестовой БД не будет.
> Создайте её вручную:
> `docker compose exec postgres createdb -U tg_bot tg_bot_test`

## Миграции

```powershell
alembic revision --autogenerate -m "описание"   # после изменения моделей
alembic upgrade head
alembic downgrade -1
```

Сгенерированную миграцию всегда просматривайте вручную. Новое значение
PostgreSQL enum (например, новый тип документа) добавляется миграцией с
`ALTER TYPE ... ADD VALUE`.

## Структура

```
src/app/
├── config.py            # настройки из .env
├── main_bot.py          # точка входа бота
├── bot/                 # Telegram-слой: хендлеры, клавиатуры, middleware, тексты
├── domain/enums.py      # доменные перечисления
├── db/                  # модели SQLAlchemy, сессии
├── services/            # бизнес-логика без зависимости от Telegram
│   ├── users.py
│   ├── payments/base.py # интерфейс платёжного шлюза
│   └── storage/         # интерфейс файлового хранилища + локальная реализация
├── llm/base.py          # интерфейс LLM-клиента
├── generation/          # контракт DocumentContent + интерфейс генератора
└── rendering/base.py    # интерфейс рендерера (DocumentContent → файл)
```

Слой `bot/` вызывает `services/`, а генерация и рендеринг не знают про Telegram.
Новый тип документа добавляется как новые генератор и рендерер без изменения ядра.

## Деньги и баланс

- Все суммы хранятся в целых копейках.
- `balance_transactions` — журнал операций и **источник истины**; записи только добавляются.
- `users.balance` — денормализованный кэш суммы журнала. Он меняется только в той же
  транзакции БД, что и запись в журнал. Ограничение `CHECK (balance >= 0)` не даёт уйти в минус.
- Уникальный индекс `(order_id, reason)` не допускает двойного списания или возврата по заказу,
  уникальность `(provider, provider_payment_id)` — повторного зачисления одного платежа.
