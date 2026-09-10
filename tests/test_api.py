"""Сквозные проверки HTTP: доступ, каталог, комнаты, трекинг."""

from __future__ import annotations

from httpx import AsyncClient


async def test_healthz_is_open(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_landing_for_anonymous(client: AsyncClient) -> None:
    response = await client.get("/", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert "Смотрите аниме" in response.text


async def test_api_requires_login(client: AsyncClient) -> None:
    response = await client.get("/api/anime/search?q=наруто")
    assert response.status_code == 401
    assert response.json()["login_url"] == "/login"


async def test_page_redirects_to_login(client: AsyncClient) -> None:
    response = await client.get("/library", headers={"accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_guest_login_then_search(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/api/anime/search?q=тест")
    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["title"] == "Тестовое аниме"


async def test_short_queries_are_rejected(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/api/anime/search?q=a")
    assert response.status_code == 422


async def test_anime_details(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/api/anime/7")
    assert response.status_code == 200
    payload = response.json()
    assert payload["episodes"] == [1, 2, 3]
    assert payload["players"][0]["label"] == "AniLibria"


async def test_create_empty_room_and_read_it(auth_client: AsyncClient) -> None:
    created = await auth_client.post("/api/rooms", json={"name": "Вечер аниме"})
    assert created.status_code == 201
    code = created.json()["code"]

    state = await auth_client.get(f"/api/rooms/{code}")
    assert state.status_code == 200
    assert state.json()["name"] == "Вечер аниме"
    assert state.json()["source"] is None
    assert state.json()["playback"]["playing"] is False


async def test_missing_room_is_404(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/api/rooms/nosuchroom")
    assert response.status_code == 404


async def test_private_rooms_are_not_listed(auth_client: AsyncClient) -> None:
    await auth_client.post("/api/rooms", json={"name": "Только свои", "is_public": False})
    listing = await auth_client.get("/api/rooms")
    assert listing.status_code == 200
    assert listing.json()["rooms"] == []  # без зрителей комнаты не показываются


async def test_only_host_can_close_a_room(auth_client: AsyncClient) -> None:
    code = (await auth_client.post("/api/rooms", json={})).json()["code"]

    # новая гостевая сессия — это другой пользователь
    await auth_client.post("/auth/guest", data={"display_name": "Второй", "next": "/"})
    response = await auth_client.delete(f"/api/rooms/{code}")
    assert response.status_code == 403


async def test_room_page_renders(auth_client: AsyncClient) -> None:
    code = (await auth_client.post("/api/rooms", json={"name": "Комната"})).json()["code"]
    page = await auth_client.get(f"/room/{code}", headers={"accept": "text/html"})
    assert page.status_code == 200
    assert 'id="room-config"' in page.text
    assert code in page.text


async def test_unknown_room_page_shows_error(auth_client: AsyncClient) -> None:
    page = await auth_client.get("/room/zzzzzzz", headers={"accept": "text/html"})
    assert page.status_code == 200
    assert "Комната не найдена" in page.text


async def test_tracking_round_trip(auth_client: AsyncClient) -> None:
    ref = {"anime_id": "7", "source": "animego", "title": "Тестовое аниме"}

    saved = await auth_client.put(
        "/api/tracking/progress", json={**ref, "episode": 1, "position": 1400, "duration": 1440}
    )
    assert saved.status_code == 200
    assert saved.json()["completed"] is True

    listing = await auth_client.get("/api/tracking/list")
    assert listing.json()["counts"]["watching"] == 1

    rated = await auth_client.put("/api/tracking/rating", json={**ref, "rating": 8})
    assert rated.json()["rating"] == 8

    stats = await auth_client.get("/api/tracking/stats")
    assert stats.json()["episodes_watched"] == 1

    removed = await auth_client.request("DELETE", "/api/tracking/entry", json=ref)
    assert removed.status_code == 200
    assert (await auth_client.get("/api/tracking/list")).json()["entries"] == []


async def test_invalid_rating_is_rejected(auth_client: AsyncClient) -> None:
    response = await auth_client.put("/api/tracking/rating", json={"anime_id": "7", "rating": 99})
    assert response.status_code == 422


async def test_stream_proxy_needs_a_valid_token(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/api/stream/playlist?t=подделка")
    assert response.status_code == 403


async def test_logout_clears_the_session(auth_client: AsyncClient) -> None:
    await auth_client.post("/auth/logout")
    response = await auth_client.get("/auth/me")
    assert response.json() == {"authenticated": False}
