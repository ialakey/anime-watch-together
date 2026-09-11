"""Подписки на новые серии и рассылка через Discord-бота."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.config import Settings
from app.db import models
from app.deps import require_admin
from app.services.catalog import Catalog
from app.services.notifications import NotificationService, resolve_recipient
from app.services.tracking import AnimeRef

REF = AnimeRef(source="fake", anime_id="7", title="Тестовое аниме", poster="https://p/1.jpg")


@pytest.fixture
def settings() -> Settings:
    """Те же настройки, что и в conftest, но с включёнными уведомлениями."""
    return Settings(
        secret_key="test-secret-key-000",
        database_url="sqlite+aiosqlite:///:memory:",
        discord_auth_enabled=False,
        notifications_enabled=True,
        discord_bot_token="bot-token",
    )


@pytest.fixture
def service(settings: Settings, catalog: Catalog, fake_notifier) -> NotificationService:
    return NotificationService(settings, catalog, notifier=fake_notifier)


async def _discord_user(db, *, external_id: str = "42", enabled: bool = True) -> models.User:
    user = models.User(
        provider="discord",
        external_id=external_id,
        username="tanuki",
        display_name="Тануки",
        notifications_enabled=enabled,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ------------------------------------------------------------------ подписки


async def test_subscribe_remembers_current_episode(db, service, user: models.User):
    """База — последняя вышедшая серия, чтобы не слать уведомления задним числом."""
    subscription = await service.subscribe(db, user.id, REF)

    assert subscription.last_known_episode == 3
    assert subscription.title == "Тестовое аниме"


async def test_subscribe_is_idempotent(db, service, user: models.User):
    await service.subscribe(db, user.id, REF)
    await service.subscribe(db, user.id, REF)

    assert len(await service.list_subscriptions(db, user.id)) == 1


async def test_unsubscribe_removes_the_row(db, service, user: models.User):
    await service.subscribe(db, user.id, REF)
    await service.unsubscribe(db, user.id, REF)

    assert await service.list_subscriptions(db, user.id) == []


async def test_settings_reject_non_numeric_channel(db, service, user: models.User):
    with pytest.raises(ValueError, match="число"):
        await service.set_user_settings(db, user.id, channel_id="общий-чат")


# ------------------------------------------------------------------ рассылка


async def test_new_episode_notifies_subscriber(db, service, fake_source, fake_notifier):
    person = await _discord_user(db)
    await service.subscribe(db, person.id, REF)
    fake_source.episodes_list = [1, 2, 3, 4]
    service.catalog._episodes_cache.clear()

    sent = await service.check_once(db)

    assert sent == 1
    recipient, payload = fake_notifier.sent[0]
    assert recipient.kind == "dm"
    assert recipient.target == "42"
    assert "4" in payload["embeds"][0]["description"]


async def test_same_episode_notifies_once(db, service, fake_source, fake_notifier):
    person = await _discord_user(db)
    await service.subscribe(db, person.id, REF)
    fake_source.episodes_list = [1, 2, 3, 4]
    service.catalog._episodes_cache.clear()

    await service.check_once(db)
    await service.check_once(db)

    assert len(fake_notifier.sent) == 1


async def test_first_check_only_sets_the_baseline(db, service, fake_notifier, user: models.User):
    """Если базу снять не удалось, первая проверка молчит — она её и снимает."""
    subscription = await service.subscribe(db, user.id, REF)
    subscription.last_known_episode = 0
    await db.commit()

    sent = await service.check_once(db)

    assert sent == 0
    assert fake_notifier.sent == []
    await db.refresh(subscription)
    assert subscription.last_known_episode == 3


async def test_disabled_user_gets_nothing(db, service, fake_source, fake_notifier):
    person = await _discord_user(db, enabled=False)
    await service.subscribe(db, person.id, REF)
    fake_source.episodes_list = [1, 2, 3, 4]
    service.catalog._episodes_cache.clear()

    assert await service.check_once(db) == 0
    assert fake_notifier.sent == []


async def test_guest_without_channel_is_skipped(db, service, fake_source, fake_notifier, user):
    await service.subscribe(db, user.id, REF)
    fake_source.episodes_list = [1, 2, 3, 4]
    service.catalog._episodes_cache.clear()

    assert await service.check_once(db) == 0
    assert fake_notifier.sent == []


async def test_personal_channel_wins_over_dm(db, service, settings: Settings):
    person = await _discord_user(db)
    await service.set_user_settings(db, person.id, channel_id="12345")

    recipient = resolve_recipient(person, settings)

    assert recipient.kind == "channel"
    assert recipient.target == "12345"
    assert recipient.mention == "<@42>"


async def test_episode_message_links_to_the_episode(db, service, user: models.User):
    subscription = await service.subscribe(db, user.id, REF)

    payload = service.episode_message(subscription, 4)

    embed = payload["embeds"][0]
    assert embed["url"].endswith("/anime/7?episode=4")
    assert embed["thumbnail"]["url"] == "https://p/1.jpg"


# ----------------------------------------------------------------------- API


async def test_api_subscribe_and_list(auth_client: AsyncClient):
    response = await auth_client.post(
        "/api/notifications/subscriptions",
        json={"anime_id": "7", "source": "fake", "title": "Тестовое аниме"},
    )

    assert response.status_code == 200
    payload = (await auth_client.get("/api/notifications/subscriptions")).json()
    assert [item["anime_id"] for item in payload["subscriptions"]] == ["7"]


async def test_api_unsubscribe(auth_client: AsyncClient):
    await auth_client.post(
        "/api/notifications/subscriptions", json={"anime_id": "7", "source": "fake"}
    )

    response = await auth_client.request(
        "DELETE", "/api/notifications/subscriptions", json={"anime_id": "7", "source": "fake"}
    )

    assert response.status_code == 200
    payload = (await auth_client.get("/api/notifications/subscriptions")).json()
    assert payload["subscriptions"] == []


async def test_api_settings_toggle(auth_client: AsyncClient):
    response = await auth_client.put("/api/notifications/settings", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False


async def test_api_test_message_needs_a_destination(auth_client: AsyncClient):
    """Гостю без канала писать некуда — об этом и говорим."""
    response = await auth_client.post("/api/notifications/test")

    assert response.status_code == 400
    assert "канала" in response.json()["detail"]


async def test_api_broadcast_requires_admin(auth_client: AsyncClient):
    response = await auth_client.post(
        "/api/notifications/broadcast",
        json={"anime_id": "7", "source": "fake", "user_ids": [1]},
    )

    assert response.status_code == 403


async def test_api_broadcast_subscribes_chosen_people(auth_client: AsyncClient, db):
    app = auth_client.app
    me = (await auth_client.get("/auth/me")).json()["user"]
    app.dependency_overrides[require_admin] = lambda: me

    response = await auth_client.post(
        "/api/notifications/broadcast",
        json={
            "anime_id": "7",
            "source": "fake",
            "title": "Тестовое аниме",
            "user_ids": [me["id"]],
        },
    )
    app.dependency_overrides.pop(require_admin)

    assert response.status_code == 200
    assert response.json() == {"status": "subscribed", "count": 1}
    payload = (await auth_client.get("/api/notifications/subscriptions")).json()
    assert [item["anime_id"] for item in payload["subscriptions"]] == ["7"]


async def test_notifications_page_renders(auth_client: AsyncClient):
    response = await auth_client.get("/notifications")

    assert response.status_code == 200
    assert "Отслеживаемые тайтлы" in response.text


async def test_api_reports_disabled_notifications(auth_client: AsyncClient):
    auth_client.app.state.settings.notifications_enabled = False
    try:
        response = await auth_client.get("/api/notifications/subscriptions")
    finally:
        auth_client.app.state.settings.notifications_enabled = True

    assert response.status_code == 503
