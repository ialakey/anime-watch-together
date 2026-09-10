"""Каталог и сборка источника для плеера."""

from __future__ import annotations

import pytest
from anime_dl_core import PlayerResult, SkipSegment, Stream, StreamKind

from app.config import Settings
from app.services.cache import TTLCache
from app.services.catalog import Catalog, CatalogError, NothingFound
from app.services.playback import PlaybackService
from app.services.streaming import StreamProxy

HEADERS = {"Referer": "https://aniboom.one/"}


def player_result() -> PlayerResult:
    return PlayerResult(
        player="aniboom",
        source_url="https://aniboom.one/embed/x",
        title="Тестовое аниме",
        poster="https://p/1.jpg",
        duration=1440,
        streams=[
            Stream(url="https://cdn/master.m3u8", kind=StreamKind.HLS, headers=HEADERS),
            Stream(url="https://cdn/1080.m3u8", kind=StreamKind.HLS, quality=1080, headers=HEADERS),
            Stream(url="https://cdn/720.m3u8", kind=StreamKind.HLS, quality=720, headers=HEADERS),
            Stream(url="https://cdn/2160.m3u8", kind=StreamKind.HLS, quality=2160, headers=HEADERS),
        ],
        skip_segments=[SkipSegment(start=60, end=150, kind="opening")],
    )


async def test_search_is_cached(catalog: Catalog, fake_source) -> None:
    await catalog.search("тест")
    await catalog.search("ТЕСТ")  # регистр не важен — тот же ключ кэша
    assert fake_source.searches == ["тест"]


async def test_short_queries_are_rejected(catalog: Catalog) -> None:
    with pytest.raises(CatalogError) as error:
        await catalog.search("a")
    assert error.value.status == 400


async def test_details_falls_back_to_the_first_episode(catalog: Catalog) -> None:
    details = await catalog.details("7", episode=99)
    assert details.episode == 1
    assert details.episodes == [1, 2, 3]
    assert details.card.title == "Тестовое аниме"


async def test_pick_player_prefers_the_requested_key(catalog: Catalog) -> None:
    option = await catalog.pick_player("7", 1, "kodik::anidub")
    assert option.player == "Kodik"


async def test_pick_player_matches_translation_across_players(catalog: Catalog) -> None:
    """Если озвучка переехала на другой плеер — держимся за озвучку."""
    option = await catalog.pick_player("7", 1, "sibnet::anilibria")
    assert option.label == "AniLibria"


async def test_pick_player_falls_back_to_the_first(catalog: Catalog) -> None:
    option = await catalog.pick_player("7", 1, "нет::такого")
    assert option.player == "AniBoom"


async def test_build_source_proxies_and_sorts_streams(
    settings: Settings, catalog: Catalog, monkeypatch
) -> None:
    async def fake_resolve(option):
        return player_result()

    monkeypatch.setattr(catalog, "resolve", fake_resolve)
    playback = PlaybackService(settings, catalog, StreamProxy(settings))

    source = await playback.build_source("7", 2)
    payload = source.to_dict()

    assert payload["episode"] == 2
    assert payload["translation"] == "AniLibria"
    assert payload["duration"] == 1440
    assert payload["skip_segments"] == [{"start": 60, "end": 150, "kind": "opening"}]

    # 2160p выше потолка STREAM_MAX_QUALITY и отсеян
    qualities = [stream["quality"] for stream in payload["streams"]]
    assert 2160 not in qualities
    # первым идёт мастер-плейлист: качество выберет сам плеер
    assert payload["streams"][0]["quality"] is None
    assert qualities[1:] == [1080, 720]

    # наружу уходят только проксированные ссылки
    assert all(stream["url"].startswith("/api/stream/") for stream in payload["streams"])
    assert "cdn" not in payload["streams"][0]["url"]


async def test_build_source_keeps_title_from_the_card(
    settings: Settings, catalog: Catalog, monkeypatch
) -> None:
    async def fake_resolve(option):
        result = player_result()
        result.title = None
        return result

    monkeypatch.setattr(catalog, "resolve", fake_resolve)
    playback = PlaybackService(settings, catalog, StreamProxy(settings))
    source = await playback.build_source("7", 1)
    assert source.anime_title == "Тестовое аниме"


async def test_empty_players_raise_nothing_found(catalog: Catalog, monkeypatch) -> None:
    async def no_players(anime_id: str, episode: int):
        return []

    monkeypatch.setattr(catalog, "players", no_players)
    with pytest.raises(NothingFound):
        await catalog.pick_player("7", 1)


# ------------------------------------------------------------------- кэш


async def test_ttl_cache_expires() -> None:
    cache: TTLCache[str] = TTLCache(ttl=0.05)
    cache.set("k", "v")
    assert cache.get("k") == "v"
    import asyncio

    await asyncio.sleep(0.08)
    assert cache.get("k") is None


async def test_ttl_cache_computes_once_under_load() -> None:
    import asyncio

    cache: TTLCache[int] = TTLCache(ttl=10)
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return 42

    results = await asyncio.gather(*(cache.get_or_set("k", factory) for _ in range(5)))
    assert results == [42] * 5
    assert calls == 1


def test_ttl_cache_evicts_when_full() -> None:
    cache: TTLCache[int] = TTLCache(ttl=10, max_size=4)
    for index in range(10):
        cache.set(index, index)
    assert len(cache) <= 4
