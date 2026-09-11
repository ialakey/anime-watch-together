"""Разбор пути к файлу sqlite: относительный и абсолютный URL — разные вещи."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.session import sqlite_path


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # три слэша — путь относительно текущего каталога
        ("sqlite+aiosqlite:///./data/anime_watch.db", "data/anime_watch.db"),
        ("sqlite+aiosqlite:///anime.db", "anime.db"),
        # четыре слэша — абсолютный путь; именно так БД лежит в контейнере
        ("sqlite+aiosqlite:////data/anime_watch.db", "/data/anime_watch.db"),
        ("sqlite:////var/lib/awt/db.sqlite3", "/var/lib/awt/db.sqlite3"),
    ],
)
def test_sqlite_path(url: str, expected: str) -> None:
    path = sqlite_path(url)
    assert path is not None
    assert PurePosixPath(path.as_posix()) == PurePosixPath(expected)


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///:memory:",
        "sqlite://",
        "postgresql+asyncpg://anime:anime@db:5432/anime",
    ],
)
def test_no_file_to_create(url: str) -> None:
    assert sqlite_path(url) is None


async def test_init_models_adds_missing_columns(tmp_path) -> None:
    """Старую базу без колонок уведомлений догоняем на месте: sqlite живёт без Alembic."""
    from sqlalchemy import select, text

    from app.db import session as db_session
    from app.db.models import User

    db_file = tmp_path / "old.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE users (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    provider VARCHAR(16) NOT NULL,
                    external_id VARCHAR(64) NOT NULL,
                    username VARCHAR(64) NOT NULL,
                    display_name VARCHAR(64) NOT NULL,
                    avatar_url VARCHAR(255),
                    is_admin BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL,
                    last_seen_at DATETIME NOT NULL
                )
                """
            )
        )
        await connection.execute(
            text(
                "INSERT INTO users (provider, external_id, username, display_name, is_admin,"
                " created_at, last_seen_at) VALUES ('guest', 'old', 'Старожил', 'Старожил', 0,"
                " '2024-01-01', '2024-01-01')"
            )
        )
    await engine.dispose()

    db_session._engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    db_session._session_factory = None
    try:
        await db_session.init_models()
        async with db_session.get_session_factory()() as session:
            user = (await session.execute(select(User))).scalar_one()
            assert user.display_name == "Старожил"
            assert user.notifications_enabled is True
            assert user.notify_channel_id is None
    finally:
        await db_session.dispose_engine()
