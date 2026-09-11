"""Общие фикстуры. Ни один тест не ходит в сеть: источники подменены заглушками."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-000")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("DISCORD_AUTH_ENABLED", "false")

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.db import models
from app.db import session as db_session
from app.services.catalog import AnimeCard, Catalog, PlayerOption


@pytest.fixture
def settings() -> Settings:
    return Settings(
        secret_key="test-secret-key-000",
        database_url="sqlite+aiosqlite:///:memory:",
        discord_auth_enabled=False,
    )


@pytest.fixture
async def db() -> AsyncIterator:
    """Чистая in-memory БД на каждый тест."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(models.Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def user(db) -> models.User:
    user = models.User(
        provider="guest",
        external_id="tester",
        username="tester",
        display_name="Тестер",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


class FakeSource:
    """Источник каталога без сети."""

    name = "fake"

    def __init__(self) -> None:
        self.searches: list[str] = []
        self.episodes_list = [1, 2, 3]

    async def search(self, query: str, limit: int) -> list[AnimeCard]:
        self.searches.append(query)
        return [
            AnimeCard(id="7", title="Тестовое аниме", source=self.name, poster="https://p/1.jpg")
        ][:limit]

    async def card(self, anime_id: str) -> AnimeCard:
        return AnimeCard(id=anime_id, title="Тестовое аниме", source=self.name)

    async def episodes(self, anime_id: str) -> list[int]:
        return list(self.episodes_list)

    async def players(self, anime_id: str, episode: int) -> list[PlayerOption]:
        return [
            PlayerOption(
                key="aniboom::anilibria",
                player="AniBoom",
                label="AniLibria",
                embed=f"https://aniboom.one/embed/x?episode={episode}",
                episode=episode,
            ),
            PlayerOption(
                key="kodik::anidub",
                player="Kodik",
                label="AniDub",
                embed=f"https://kodik.info/serial/1/x/720?episode={episode}",
                episode=episode,
            ),
        ]

    def close(self) -> None:
        pass


@pytest.fixture
def fake_source() -> FakeSource:
    return FakeSource()


@pytest.fixture
def catalog(settings: Settings, fake_source: FakeSource) -> Catalog:
    return Catalog(settings, source=fake_source)


class FakeNotifier:
    """Бот-заглушка: складывает сообщения в список вместо похода в Discord."""

    def __init__(self) -> None:
        self.sent: list[tuple[object, dict]] = []

    @property
    def enabled(self) -> bool:
        return True

    async def send(self, recipient, payload) -> bool:
        self.sent.append((recipient, payload))
        return True

    async def aclose(self) -> None:
        pass


@pytest.fixture
def fake_notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
async def client(
    settings: Settings, catalog: Catalog, fake_notifier: FakeNotifier
) -> AsyncIterator[AsyncClient]:
    """Приложение целиком, но с подменённым каталогом, ботом и временной БД."""
    from app.main import create_app

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(models.Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db_session._engine = engine
    db_session._session_factory = factory

    app = create_app(settings)
    app.state.catalog = catalog
    app.state.playback.catalog = catalog
    app.state.notifications.catalog = catalog
    app.state.notifications.notifier = fake_notifier
    # ручки берут настройки через зависимость — пусть это будет тот же объект,
    # что и у сервисов, иначе тесты меняют одни настройки, а код читает другие
    app.dependency_overrides[get_settings] = lambda: settings

    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://test") as http,
    ):
        # тестам иногда нужно само приложение: подменить зависимость, дёрнуть сервис
        http.app = app
        yield http

    await engine.dispose()
    db_session._engine = None
    db_session._session_factory = None


@pytest.fixture
async def auth_client(client: AsyncClient) -> AsyncClient:
    """Клиент с гостевой сессией."""
    response = await client.post("/auth/guest", data={"display_name": "Тестер", "next": "/"})
    assert response.status_code in (303, 307)
    return client


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
