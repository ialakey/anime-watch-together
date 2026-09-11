"""Асинхронный движок SQLAlchemy и фабрика сессий."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import MetaData, inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateColumn

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def sqlite_path(database_url: str) -> Path | None:
    """Путь к файлу sqlite или ``None``, если это не файловая sqlite.

    В URL SQLAlchemy ровно один слэш отделяет схему от пути, поэтому
    ``sqlite:///./data/x.db`` -> ``./data/x.db`` (относительный),
    а ``sqlite:////data/x.db`` -> ``/data/x.db`` (абсолютный, как в контейнере).
    Отрезать все ведущие слэши нельзя: абсолютный путь превратится в относительный.
    """
    if not database_url.startswith("sqlite"):
        return None
    raw = urlsplit(database_url).path
    if not raw or raw in ("/", "/:memory:"):
        return None
    return Path(raw[1:])


def _ensure_sqlite_dir(database_url: str) -> None:
    """Для sqlite создаём каталог файла БД — иначе движок упадёт при первом запросе."""
    path = sqlite_path(database_url)
    if path is None:
        return
    parent = path.parent
    if parent and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    _ensure_sqlite_dir(settings.database_url)
    kwargs: dict[str, object] = {"echo": settings.db_echo, "future": True}
    if not settings.is_sqlite:
        kwargs |= {"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True}
    return create_async_engine(settings.database_url, **kwargs)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """Зависимость FastAPI: сессия на один запрос."""
    async with get_session_factory()() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия для фонового кода (WebSocket, задачи): коммитит или откатывает сама."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def init_models() -> None:
    """Создаёт таблицы напрямую, без Alembic (используется в тестах и dev-режиме)."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(_metadata().create_all)
        await conn.run_sync(_add_missing_columns)
    log.info("Схема БД синхронизирована")


def _metadata() -> MetaData:
    from app.db.models import Base

    return Base.metadata


def _add_missing_columns(connection: Connection) -> None:
    """Догоняет схему уже существующей базы: новые колонки в старых таблицах.

    ``create_all`` создаёт только целиком отсутствующие таблицы, а sqlite-режим
    живёт без Alembic. Без этого шага обновление приложения ломало бы базу,
    которая уже есть: первый же SELECT спросил бы колонку, которой нет.
    """
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    for table in _metadata().sorted_tables:
        if table.name not in existing:
            continue
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            ddl = CreateColumn(column).compile(connection.engine).string
            connection.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}')
            log.info("Добавлена колонка %s.%s", table.name, column.name)
