"""Проксирование видео через сервер.

Зачем прокси вообще нужен:

* CDN (Kodik, Aniboom, Sibnet) отдаёт файл только с правильным ``Referer`` —
  из браузера такой заголовок не подставить;
* прямая ссылка привязана к IP, который её получил, то есть к нашему серверу;
* ссылки живут недолго, а прокси-ссылка стабильна всё время просмотра.

Как это устроено: на каждую внешнюю ссылку выписывается подписанный токен
(HMAC поверх URL + заголовков + срока годности). Без подписи прокси ничего не
скачает, так что открытым релеем он не становится. HLS-плейлисты переписываются
на лету: все вложенные URI заменяются на ссылки того же прокси.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

PLAYLIST_MOUNT = "/api/stream/playlist"
SEGMENT_MOUNT = "/api/stream/segment"

#: Заголовки, которые имеет смысл пробрасывать от браузера к CDN.
_FORWARD_REQUEST_HEADERS = ("range",)
#: Заголовки ответа CDN, важные для плеера. Content-Type проставляется отдельно,
#: иначе он уедет в ответ дважды.
_FORWARD_RESPONSE_HEADERS = (
    "content-length",
    "content-range",
    "accept-ranges",
    "last-modified",
    "etag",
)

#: Строки плейлиста, у которых URI спрятан в атрибуте ``URI="..."``.
_URI_ATTR_TAGS = ("#EXT-X-KEY", "#EXT-X-MAP", "#EXT-X-MEDIA", "#EXT-X-I-FRAME-STREAM-INF")


class StreamTokenError(Exception):
    """Токен подделан, испорчен или протух."""


@dataclass(frozen=True)
class StreamTarget:
    """Расшифрованная цель прокси."""

    url: str
    headers: dict[str, str]
    is_playlist: bool


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class StreamProxy:
    """Выписывает подписанные ссылки и отдаёт по ним контент."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._secret = settings.secret_key.encode("utf-8")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, read=60.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
        )
        self._own_client = client is None

    # ------------------------------------------------------------- токены
    def sign(self, url: str, headers: dict[str, str], *, is_playlist: bool) -> str:
        payload = {
            "u": url,
            "h": headers or {},
            "p": 1 if is_playlist else 0,
            "e": int(time.time()) + self.settings.stream_token_ttl,
        }
        body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest()
        return f"{body}.{_b64encode(signature)}"

    def verify(self, token: str) -> StreamTarget:
        try:
            body, signature = token.split(".", 1)
        except ValueError as exc:
            raise StreamTokenError("Ссылка на видео повреждена.") from exc
        expected = hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64decode(signature), expected):
            raise StreamTokenError("Подпись ссылки на видео неверна.")
        try:
            payload: dict[str, Any] = json.loads(_b64decode(body))
        except (ValueError, UnicodeDecodeError) as exc:
            raise StreamTokenError("Ссылка на видео повреждена.") from exc
        if int(payload.get("e", 0)) < time.time():
            raise StreamTokenError("Ссылка на видео устарела — обновите страницу.")
        url = str(payload.get("u", ""))
        if urlsplit(url).scheme not in ("http", "https"):
            raise StreamTokenError("Недопустимый адрес видео.")
        return StreamTarget(
            url=url,
            headers={str(k): str(v) for k, v in (payload.get("h") or {}).items()},
            is_playlist=bool(payload.get("p")),
        )

    def proxy_url(self, url: str, headers: dict[str, str], *, is_playlist: bool) -> str:
        mount = PLAYLIST_MOUNT if is_playlist else SEGMENT_MOUNT
        return f"{mount}?t={self.sign(url, headers, is_playlist=is_playlist)}"

    def playable_url(self, url: str, headers: dict[str, str], kind: str) -> str:
        """Ссылка, которую можно отдать браузеру."""
        if not self.settings.stream_proxy_enabled:
            return url
        return self.proxy_url(url, headers, is_playlist=kind == "hls")

    # ---------------------------------------------------------- плейлисты
    async def fetch_playlist(self, target: StreamTarget) -> str:
        response = await self._client.get(target.url, headers=target.headers)
        response.raise_for_status()
        return self.rewrite_playlist(response.text, str(response.url), target.headers)

    def rewrite_playlist(self, content: str, playlist_url: str, headers: dict[str, str]) -> str:
        """Заменяет все URI внутри m3u8 на ссылки этого же прокси."""
        lines: list[str] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                lines.append(raw_line)
            elif line.startswith("#"):
                lines.append(self._rewrite_tag(line, playlist_url, headers))
            else:
                absolute = urljoin(playlist_url, line)
                lines.append(
                    self.proxy_url(absolute, headers, is_playlist=_looks_like_playlist(absolute))
                )
        return "\n".join(lines) + "\n"

    def _rewrite_tag(self, line: str, playlist_url: str, headers: dict[str, str]) -> str:
        if not any(line.startswith(tag) for tag in _URI_ATTR_TAGS) or 'URI="' not in line:
            return line
        head, _, rest = line.partition('URI="')
        uri, _, tail = rest.partition('"')
        if not uri:
            return line
        absolute = urljoin(playlist_url, uri)
        # ключи шифрования и i-frame плейлисты тоже должны идти через прокси
        proxied = self.proxy_url(absolute, headers, is_playlist=_looks_like_playlist(absolute))
        return f'{head}URI="{proxied}"{tail}'

    # ------------------------------------------------------------ сегменты
    def stream_request(self, target: StreamTarget, request_headers: dict[str, str]):
        """Контекст httpx со стримовым ответом CDN (не забудьте ``async with``)."""
        headers = dict(target.headers)
        for name in _FORWARD_REQUEST_HEADERS:
            value = request_headers.get(name)
            if value:
                headers[name] = value
        return self._client.stream("GET", target.url, headers=headers)

    @staticmethod
    def response_headers(upstream: httpx.Response) -> dict[str, str]:
        headers = {
            name: value
            for name, value in upstream.headers.items()
            if name.lower() in _FORWARD_RESPONSE_HEADERS
        }
        headers.setdefault("Accept-Ranges", "bytes")
        headers["Cache-Control"] = "private, max-age=30"
        return headers

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()


def _looks_like_playlist(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith((".m3u8", ".m3u"))
