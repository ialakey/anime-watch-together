"""Сборка «источника» для плеера: каталог + прокси в одном месте.

Комната и REST API просят одно и то же — «дай всё нужное, чтобы включить
серию N», поэтому логика живёт отдельно от них обоих.
"""

from __future__ import annotations

import logging
from typing import Any

from anime_dl_core import PlayerResult, Stream

from app.config import Settings
from app.services.catalog import AnimeCard, Catalog, NothingFound, SourceInfo, StreamInfo
from app.services.streaming import StreamProxy

log = logging.getLogger(__name__)


class PlaybackService:
    """Готовит ссылки для плеера и держит их в понятном клиенту виде."""

    def __init__(self, settings: Settings, catalog: Catalog, streams: StreamProxy) -> None:
        self.settings = settings
        self.catalog = catalog
        self.streams = streams

    async def build_source(
        self,
        anime_id: str,
        episode: int,
        *,
        player_key: str | None = None,
        card: AnimeCard | None = None,
    ) -> SourceInfo:
        option = await self.catalog.pick_player(anime_id, episode, player_key)
        result = await self.catalog.resolve(option)
        card = card or await self._card_or_none(anime_id)
        title = (card.title if card else None) or result.title or f"Аниме #{anime_id}"
        poster = (card.poster if card else None) or result.poster
        return SourceInfo(
            anime_id=anime_id,
            anime_title=title,
            episode=episode,
            player_key=option.key,
            player=option.player,
            translation=option.label,
            poster=poster,
            duration=result.duration,
            streams=self._streams_for_client(result),
            skip_segments=[segment.to_dict() for segment in result.skip_segments],
        )

    async def _card_or_none(self, anime_id: str) -> AnimeCard | None:
        """Карточка нужна только ради названия и постера — без неё тоже смотрим."""
        try:
            return await self.catalog.card(anime_id)
        except Exception:
            log.debug("Карточка %s недоступна", anime_id, exc_info=True)
            return None

    def _streams_for_client(self, result: PlayerResult) -> list[StreamInfo]:
        """Отбирает дорожки и подменяет ссылки на прокси-ссылки.

        Первым идёт то, что стоит включать по умолчанию: мастер-плейлист HLS
        (плеер сам подберёт качество), иначе самое высокое доступное качество.
        """
        candidates = [
            stream
            for stream in result.streams
            if stream.quality is None or stream.quality <= self.settings.stream_max_quality
        ]
        if not candidates:
            # всё, что есть, выше потолка — берём самое низкое из имеющегося
            candidates = sorted(result.streams, key=lambda stream: stream.quality or 0)[:1]
        if not candidates:
            raise NothingFound("Плеер не отдал ни одной дорожки.")

        candidates.sort(key=_stream_rank, reverse=True)
        return [self._to_info(stream) for stream in candidates]

    def _to_info(self, stream: Stream) -> StreamInfo:
        headers = dict(stream.headers)
        return StreamInfo(
            url=self.streams.playable_url(stream.url, headers, stream.kind.value),
            kind=stream.kind.value,
            quality=stream.quality,
            label=stream.label,
            direct_url=stream.url,
            headers=headers,
        )


def _stream_rank(stream: Stream) -> tuple[int, int, int]:
    """Мастер-плейлист вперёд, дальше по убыванию качества."""
    kind_bonus = {"hls": 2, "mp4": 1, "dash": 0}
    return (
        1 if stream.is_master else 0,
        stream.quality or 0,
        kind_bonus.get(stream.kind.value, 0),
    )


def source_to_ref(source: dict[str, Any] | None) -> tuple[str, int] | None:
    """``(anime_id, episode)`` из снимка источника комнаты."""
    if not source:
        return None
    anime_id = source.get("anime_id")
    episode = source.get("episode")
    if not anime_id or not episode:
        return None
    return str(anime_id), int(episode)
