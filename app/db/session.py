"""Асинхронный движок SQLAlchemy и фабрика сессий."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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
    from app.db.models import Base

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("Схема БД синхронизирована")
