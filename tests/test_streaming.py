"""Подписанные ссылки и переписывание HLS-плейлистов."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import pytest

from app.config import Settings
from app.services.streaming import StreamProxy, StreamTokenError

HEADERS = {"Referer": "https://aniboom.one/", "User-Agent": "test-agent"}


@pytest.fixture
def proxy() -> StreamProxy:
    return StreamProxy(Settings(secret_key="secret-one"))


def token_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["t"][0]


def test_token_round_trip(proxy: StreamProxy) -> None:
    token = proxy.sign("https://cdn.example/video.m3u8", HEADERS, is_playlist=True)
    target = proxy.verify(token)
    assert target.url == "https://cdn.example/video.m3u8"
    assert target.headers == HEADERS
    assert target.is_playlist is True


def test_tampered_token_is_rejected(proxy: StreamProxy) -> None:
    token = proxy.sign("https://cdn.example/a.ts", {}, is_playlist=False)
    body, signature = token.split(".", 1)
    forged = proxy.sign("https://evil.example/x", {}, is_playlist=False).split(".", 1)[0]
    with pytest.raises(StreamTokenError):
        proxy.verify(f"{forged}.{signature}")
    with pytest.raises(StreamTokenError):
        proxy.verify(f"{body}.{'A' * len(signature)}")


def test_another_secret_cannot_forge_links() -> None:
    mine = StreamProxy(Settings(secret_key="secret-one"))
    theirs = StreamProxy(Settings(secret_key="secret-two"))
    token = theirs.sign("https://cdn.example/a.ts", {}, is_playlist=False)
    with pytest.raises(StreamTokenError):
        mine.verify(token)


def test_expired_token_is_rejected() -> None:
    proxy = StreamProxy(Settings(secret_key="s", stream_token_ttl=-1))
    token = proxy.sign("https://cdn.example/a.ts", {}, is_playlist=False)
    with pytest.raises(StreamTokenError, match="устарела"):
        proxy.verify(token)


def test_non_http_targets_are_rejected(proxy: StreamProxy) -> None:
    token = proxy.sign("file:///etc/passwd", {}, is_playlist=False)
    with pytest.raises(StreamTokenError, match="Недопустимый"):
        proxy.verify(token)


def test_broken_token_is_rejected(proxy: StreamProxy) -> None:
    with pytest.raises(StreamTokenError):
        proxy.verify("совсем-не-токен")


def test_playlist_rewrites_every_uri(proxy: StreamProxy) -> None:
    playlist = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x0',
            "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360",
            "360.m3u8",
            "#EXTINF:6.0,",
            "segment-1.ts",
            "#EXTINF:6.0,",
            "https://other.cdn/segment-2.ts",
            "#EXT-X-ENDLIST",
        ]
    )
    result = proxy.rewrite_playlist(playlist, "https://cdn.example/hls/master.m3u8", HEADERS)
    lines = result.splitlines()

    # теги без URI не трогаем
    assert lines[0] == "#EXTM3U"
    assert lines[1] == "#EXT-X-VERSION:3"
    assert "#EXT-X-ENDLIST" in lines

    # ключ шифрования тоже уходит через прокси
    assert "/api/stream/segment?t=" in lines[2]
    assert lines[2].startswith("#EXT-X-KEY:METHOD=AES-128,URI=")
    assert lines[2].endswith(",IV=0x0")

    # вложенный плейлист — на playlist-эндпоинт, сегменты — на segment
    assert lines[4].startswith("/api/stream/playlist?t=")
    assert lines[6].startswith("/api/stream/segment?t=")

    # относительные ссылки разворачиваются относительно плейлиста
    assert proxy.verify(token_of(lines[4])).url == "https://cdn.example/hls/360.m3u8"
    assert proxy.verify(token_of(lines[6])).url == "https://cdn.example/hls/segment-1.ts"
    # абсолютные остаются собой
    assert proxy.verify(token_of(lines[8])).url == "https://other.cdn/segment-2.ts"
    # заголовки CDN едут дальше по цепочке
    assert proxy.verify(token_of(lines[6])).headers == HEADERS


def test_playable_url_can_be_left_direct() -> None:
    proxy = StreamProxy(Settings(secret_key="s", stream_proxy_enabled=False))
    assert proxy.playable_url("https://cdn/x.m3u8", HEADERS, "hls") == "https://cdn/x.m3u8"


def test_playable_url_picks_the_right_mount(proxy: StreamProxy) -> None:
    assert proxy.playable_url("https://cdn/x.m3u8", {}, "hls").startswith("/api/stream/playlist")
    assert proxy.playable_url("https://cdn/x.mp4", {}, "mp4").startswith("/api/stream/segment")


def test_token_carries_an_expiry(proxy: StreamProxy) -> None:
    import base64
    import json

    body = proxy.sign("https://cdn/x.ts", {}, is_playlist=False).split(".", 1)[0]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert payload["e"] > time.time()
