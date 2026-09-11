"""Каталог участников и чужие профили."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.config import Settings
from app.db import models
from app.services.profiles import ProfileService
from app.services.tracking import AnimeRef, TrackingService


async def _make_user(db, name: str, external_id: str) -> models.User:
    user = models.User(provider="guest", external_id=external_id, username=name, display_name=name)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def test_directory_counts_titles_and_hours(db, settings: Settings, user: models.User):
    tracking = TrackingService(settings)
    ref = AnimeRef(source="fake", anime_id="7", title="Тестовое аниме")
    await tracking.record_progress(db, user.id, ref, episode=1, position=1800, duration=1800)

    people = await ProfileService(settings).directory(db)

    assert [person.display_name for person in people] == ["Тестер"]
    assert people[0].titles == 1
    assert people[0].episodes_watched == 1
    assert people[0].hours_watched == 0.5


async def test_directory_filters_by_name(db, settings: Settings, user: models.User):
    await _make_user(db, "Сакура", "second")
    profiles = ProfileService(settings)

    found = await profiles.directory(db, query="саку")

    assert [person.display_name for person in found] == ["Сакура"]


async def test_recent_shows_last_touched_episodes(db, settings: Settings, user: models.User):
    tracking = TrackingService(settings)
    ref = AnimeRef(source="fake", anime_id="7", title="Тестовое аниме")
    await tracking.record_progress(db, user.id, ref, episode=1, position=900, duration=1800)
    await tracking.record_progress(db, user.id, ref, episode=2, position=60, duration=1800)

    recent = await ProfileService(settings).recent(db, user.id)

    assert [item["episode"] for item in recent] == [2, 1]
    assert recent[0]["title"] == "Тестовое аниме"


async def test_users_page_lists_everyone(auth_client: AsyncClient):
    response = await auth_client.get("/users")

    assert response.status_code == 200
    assert "Тестер" in response.text


async def test_profile_page_shows_someone_elses_list(auth_client: AsyncClient):
    me = (await auth_client.get("/auth/me")).json()["user"]

    response = await auth_client.get(f"/users/{me['id']}")

    assert response.status_code == 200
    assert "Список аниме" in response.text


async def test_profile_page_404_for_unknown_user(auth_client: AsyncClient):
    response = await auth_client.get("/users/9999")

    assert response.status_code == 200  # страница ошибки рендерится как HTML
    assert "Участник не найден" in response.text


async def test_users_api_returns_summaries(auth_client: AsyncClient):
    payload = (await auth_client.get("/api/users")).json()

    assert payload["users"]
    assert payload["users"][0]["display_name"] == "Тестер"


async def test_profile_api_returns_entries(auth_client: AsyncClient):
    me = (await auth_client.get("/auth/me")).json()["user"]
    await auth_client.put(
        "/api/tracking/status",
        json={"anime_id": "7", "source": "fake", "title": "Тестовое аниме", "status": "watching"},
    )

    payload = (await auth_client.get(f"/api/users/{me['id']}")).json()

    assert payload["user"]["display_name"] == "Тестер"
    assert [entry["anime_id"] for entry in payload["entries"]] == ["7"]
    assert payload["counts"]["watching"] == 1


async def test_profiles_require_login(client: AsyncClient):
    response = await client.get("/api/users", headers={"accept": "application/json"})

    assert response.status_code == 401


@pytest.mark.parametrize("url", ["/users", "/users/1"])
async def test_pages_report_disabled_profiles(auth_client: AsyncClient, url: str):
    auth_client.app.state.settings.profiles_enabled = False
    try:
        response = await auth_client.get(url)
    finally:
        auth_client.app.state.settings.profiles_enabled = True

    assert "Профили выключены" in response.text
