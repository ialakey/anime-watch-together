"""Разбор пути к файлу sqlite: относительный и абсолютный URL — разные вещи."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

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
