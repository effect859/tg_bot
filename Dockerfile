# syntax=docker/dockerfile:1
#
# Образ бота: стадия runtime (production) и стадия test (полный pytest в Linux).
#   docker build --target runtime -t tg-bot:runtime .
#   docker build --target test -t tg-bot:test .
# Секретов в образе нет: BOT_TOKEN, ANTHROPIC_API_KEY, DATABASE_URL и т. д. — только
# через окружение при запуске (см. docker-compose.prod.yml).

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Непривилегированный пользователь: приложение не работает от root.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY alembic ./alembic
COPY src ./src


FROM base AS runtime

# Пакет — в режиме editable: код остаётся в /app/src рядом с alembic/ (так проверка
# схемы находит каталог миграций). Каталог данных принадлежит пользователю app.
RUN pip install -e . \
    && mkdir -p /app/var/storage \
    && chown -R app:app /app/var

USER app

# Метку пишет heartbeat бота (app.services.heartbeat); проверка — без БД и сети.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-m", "app.healthcheck"]

CMD ["tg-bot"]


FROM base AS test

RUN pip install -e ".[dev]" \
    && mkdir -p /app/var \
    && chown -R app:app /app/var
COPY tests ./tests

USER app

# База тестов — TEST_DATABASE_URL (PostgreSQL с базой tg_bot_test, см. README).
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
