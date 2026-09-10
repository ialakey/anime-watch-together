#!/bin/sh
# Точка входа контейнера: дожидаемся БД, накатываем миграции, запускаем приложение.
set -eu

DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:////data/anime_watch.db}"
export DATABASE_URL

log() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$1"; }

case "$DATABASE_URL" in
  sqlite*)
    log "База: SQLite"
    ;;
  *)
    log "База: внешняя, ждём готовности…"
    attempt=1
    until python - <<'PY'
import asyncio, os, sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def main() -> int:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return 0
    except Exception as exc:
        print(f"  ещё не готова: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()

raise SystemExit(asyncio.run(main()))
PY
    do
      if [ "$attempt" -ge 30 ]; then
        log "База так и не поднялась за 30 попыток — выходим"
        exit 1
      fi
      attempt=$((attempt + 1))
      sleep 2
    done
    log "База готова"
    ;;
esac

log "Применяем миграции"
alembic upgrade head

log "Запускаем: $*"
exec "$@"
