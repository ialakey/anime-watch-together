"""Каталог аниме поверх библиотеки `anime-dl-core`.

Библиотека синхронная (кроме ``extract_async``), поэтому все её блокирующие
вызовы уезжают в тредпул: FastAPI остаётся отзывчивым.

Источник выбирается настройкой ``CATALOG_SOURCE``:

* ``animego`` — поиск, список серий и набор плееров (AniBoom / CVH / Kodik / Sibnet);
* ``animedia`` — запасной вариант, когда AnimeGO закрыт Cloudflare.

Наружу оба источника отдают одинаковые структуры (:class:`AnimeCard`,
:class:`PlayerOption`), так что остальному приложению всё равно, откуда данные.
"""

from __future__ import annotations

import base64
import binascii
import html
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import anyio
from anime_dl_core import PlayerResult, extract_async
from anime_dl_core.errors import AnimeDlCoreError, NotFound, ServiceError, UnsupportedUrl
from anime_dl_core.http import HttpClient
from anime_dl_core.sources import Animedia, AnimeGo

from app.config import Settings
from app.services.cache import TTLCache

log = logging.getLogger(__name__)


class CatalogError(Exception):
    """Проблема с источником, о которой стоит сказать пользователю по-человечески."""

    def __init__(self, message: str, *, status: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class NothingFound(CatalogError):
    def __init__(self, message: str = "Ничего не найдено.") -> None:
        super().__init__(message, status=404)


@dataclass(frozen=True)
class AnimeCard:
    """Карточка аниме в выдаче поиска."""

    id: str
    title: str
    source: str
    url: str = ""
    original_title: str | None = None
    poster: str | None = None
    rating: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlayerOption:
    """Один плеер + одна озвучка для конкретного эпизода."""

    key: str
    """Стабильный ключ, по которому комната запоминает выбор зрителей."""
    player: str
    label: str
    embed: str
    episode: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnimeDetails:
    """Страница аниме: карточка + серии + плееры для выбранной серии."""

    card: AnimeCard
    episodes: list[int]
    players: list[PlayerOption] = field(default_factory=list)
    episode: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "anime": self.card.to_dict(),
            "episodes": self.episodes,
            "episode": self.episode,
            "players": [option.to_dict() for option in self.players],
        }


@dataclass(frozen=True)
class StreamInfo:
    """Готовая к проигрыванию дорожка (уже проксированная, если прокси включён)."""

    url: str
    kind: str
    quality: int | None
    label: str | None
    direct_url: str
    headers: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        # headers и direct_url наружу не отдаём: клиенту они не нужны
        return {"url": self.url, "kind": self.kind, "quality": self.quality, "label": self.label}


@dataclass(frozen=True)
class SourceInfo:
    """Всё, что нужно плееру для одной серии."""

    anime_id: str
    anime_title: str
    episode: int
    player_key: str
    player: str
    translation: str
    poster: str | None
    duration: int | None
    streams: list[StreamInfo]
    skip_segments: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "anime_id": self.anime_id,
            "anime_title": self.anime_title,
            "episode": self.episode,
            "player_key": self.player_key,
            "player": self.player,
            "translation": self.translation,
            "poster": self.poster,
            "duration": self.duration,
            "streams": [stream.to_dict() for stream in self.streams],
            "skip_segments": self.skip_segments,
        }


class CatalogSource(Protocol):
    """Минимум, который должен уметь источник каталога."""

    name: str

    async def search(self, query: str, limit: int) -> list[AnimeCard]: ...

    async def card(self, anime_id: str) -> AnimeCard: ...

    async def episodes(self, anime_id: str) -> list[int]: ...

    async def players(self, anime_id: str, episode: int) -> list[PlayerOption]: ...

    def close(self) -> None: ...


def _encode_id(value: str) -> str:
    """Упаковывает URL в компактный id, пригодный для адресной строки."""
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_id(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise CatalogError("Некорректный идентификатор аниме.", status=400) from exc


def _player_key(player: str, label: str) -> str:
    return f"{player.lower()}::{label.lower()}"


_OG_TITLE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', re.IGNORECASE)
_OG_IMAGE = re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', re.IGNORECASE)
_H1 = re.compile(r"<h1[^>]*>\s*([^<]+?)\s*</h1>", re.IGNORECASE)
#: og:title у AnimeGO выглядит как «Название (2 сезон) смотреть онлайн — Аниме».
_TITLE_TAIL = re.compile(r"\s*(?:\(\d+\s+сезон\))?\s+смотреть\s+онлайн.*$", re.IGNORECASE)


def _meta_from_html(page: str) -> tuple[str | None, str | None]:
    """Название и постер со страницы тайтла.

    ``<h1>`` точнее og:title: в og-теге к названию приклеен рекламный хвост.
    """
    title_match = _H1.search(page) or _OG_TITLE.search(page)
    image_match = _OG_IMAGE.search(page)
    title = html.unescape(title_match.group(1)).strip() if title_match else None
    if title:
        title = _TITLE_TAIL.sub("", title).strip()
    return title or None, image_match.group(1) if image_match else None


#: Идентификатор AnimeGO — «слаг-номер»; номер нужен плеерам, слаг — странице.
_ANIMEGO_NUMERIC = re.compile(r"(\d+)$")


def _animego_number(anime_id: str) -> str:
    """``naruto-uragannye-hroniki-103`` -> ``103``."""
    match = _ANIMEGO_NUMERIC.search(str(anime_id).strip())
    if not match:
        raise CatalogError(f"Некорректный идентификатор аниме: {anime_id!r}", status=400)
    return match.group(1)


class AnimeGoSource:
    """AnimeGO: основной источник — есть и серии, и несколько озвучек."""

    name = "animego"

    def __init__(self, settings: Settings) -> None:
        self._site = AnimeGo(
            mirror=settings.animego_mirror or None,
            proxy=settings.proxy_or_none,
            timeout=settings.http_timeout,
        )
        # отдельный клиент для карточки тайтла: AnimeGo такого метода не даёт
        self._http = HttpClient(proxy=settings.proxy_or_none, timeout=settings.http_timeout)

    async def search(self, query: str, limit: int) -> list[AnimeCard]:
        items = await _run(self._site.search, query, limit=limit)
        return [
            AnimeCard(
                # слаг оставляем в идентификаторе: по нему открывается страница тайтла
                id=f"{item.slug}-{item.id}",
                title=item.title,
                source=self.name,
                url=item.url,
                original_title=item.original_title,
                poster=item.poster,
                rating=item.rating,
            )
            for item in items
        ]

    async def card(self, anime_id: str) -> AnimeCard:
        """Карточка со страницы тайтла. Без слага в идентификаторе её не достать:
        ``/anime/103`` — это 103-я страница каталога, а не аниме №103."""
        number = _animego_number(anime_id)
        if anime_id == number:
            raise NothingFound(
                "Ссылка без слага — название тайтла по ней не определить. Откройте аниме из поиска."
            )
        url = f"{self._site.base_url}/anime/{anime_id}"
        response = await _run(self._http.get, url)
        if response.status != 200:
            raise NothingFound(f"Аниме {anime_id} не найдено в каталоге.")
        title, poster = _meta_from_html(response.text)
        return AnimeCard(
            id=anime_id,
            title=title or f"Аниме #{number}",
            source=self.name,
            url=response.url or url,
            poster=poster,
        )

    async def episodes(self, anime_id: str) -> list[int]:
        episodes = await _run(self._site.episodes, _animego_number(anime_id))
        return sorted(episodes) or [1]

    async def players(self, anime_id: str, episode: int) -> list[PlayerOption]:
        links = await _run(self._site.players, _animego_number(anime_id), episode)
        return [
            PlayerOption(
                key=_player_key(link.player, link.label),
                player=link.player,
                label=link.label,
                embed=link.embed,
                episode=episode,
            )
            for link in links
        ]

    def close(self) -> None:
        self._site.close()
        self._http.close()


class AnimediaSource:
    """Animedia (amd.online): страховка на случай блокировки AnimeGO.

    Идентификатор тайтла — это упакованный URL страницы, поэтому источник
    не хранит состояние между запросами.
    """

    name = "animedia"

    def __init__(self, settings: Settings) -> None:
        self._site = Animedia(
            base_url=settings.animedia_base_url,
            proxy=settings.proxy_or_none,
            timeout=settings.http_timeout,
        )

    async def search(self, query: str, limit: int) -> list[AnimeCard]:
        items = await _run(self._site.search, query, limit=limit)
        return [
            AnimeCard(id=_encode_id(item.url), title=item.title, source=self.name, url=item.url)
            for item in items
        ]

    async def card(self, anime_id: str) -> AnimeCard:
        page_url = _decode_id(anime_id)
        info = await _run(self._site.info, page_url)
        return AnimeCard(
            id=anime_id,
            title=info.title,
            source=self.name,
            url=info.url,
            poster=info.poster,
        )

    async def episodes(self, anime_id: str) -> list[int]:
        episodes = await _run(self._site.episodes, _decode_id(anime_id))
        return sorted(episodes) or [1]

    async def players(self, anime_id: str, episode: int) -> list[PlayerOption]:
        episodes = await _run(self._site.episodes, _decode_id(anime_id))
        if episode not in episodes:
            raise NothingFound(f"У этого тайтла нет серии {episode}.")
        return [
            PlayerOption(
                key=_player_key("animedia", "Animedia"),
                player="Animedia",
                label="Animedia",
                embed=episodes[episode],
                episode=episode,
            )
        ]

    def close(self) -> None:
        self._site.close()


class Catalog:
    """Фасад каталога: кэширует запросы к источнику и достаёт прямые ссылки."""

    def __init__(self, settings: Settings, source: CatalogSource | None = None) -> None:
        self.settings = settings
        self.source: CatalogSource = source or _build_source(settings)
        self._search_cache: TTLCache[list[AnimeCard]] = TTLCache(settings.catalog_cache_ttl)
        self._episodes_cache: TTLCache[list[int]] = TTLCache(settings.catalog_cache_ttl)
        self._players_cache: TTLCache[list[PlayerOption]] = TTLCache(settings.catalog_cache_ttl)
        self._card_cache: TTLCache[AnimeCard] = TTLCache(settings.catalog_cache_ttl)
        self._stream_cache: TTLCache[PlayerResult] = TTLCache(settings.stream_cache_ttl)

    @property
    def source_name(self) -> str:
        return self.source.name

    async def search(self, query: str, limit: int | None = None) -> list[AnimeCard]:
        query = query.strip()
        if len(query) < 2:
            raise CatalogError("Запрос слишком короткий — введите хотя бы 2 символа.", status=400)
        limit = limit or self.settings.search_limit
        key = (self.source_name, "search", query.lower(), limit)
        return await self._search_cache.get_or_set(
            key, lambda: _guard(self.source.search(query, limit))
        )

    async def card(self, anime_id: str) -> AnimeCard:
        key = (self.source_name, "card", anime_id)
        return await self._card_cache.get_or_set(key, lambda: _guard(self.source.card(anime_id)))

    async def episodes(self, anime_id: str) -> list[int]:
        key = (self.source_name, "episodes", anime_id)
        return await self._episodes_cache.get_or_set(
            key, lambda: _guard(self.source.episodes(anime_id))
        )

    async def players(self, anime_id: str, episode: int) -> list[PlayerOption]:
        key = (self.source_name, "players", anime_id, episode)
        return await self._players_cache.get_or_set(
            key, lambda: _guard(self.source.players(anime_id, episode))
        )

    async def details(
        self, anime_id: str, episode: int = 1, *, card: AnimeCard | None = None
    ) -> AnimeDetails:
        """Страница тайтла целиком. ``card`` можно передать из выдачи поиска."""
        episodes = await self.episodes(anime_id)
        if episode not in episodes:
            episode = episodes[0]
        players = await self.players(anime_id, episode)
        if card is None:
            card = await self._card_or_stub(anime_id, players)
        return AnimeDetails(card=card, episodes=episodes, players=players, episode=episode)

    async def _card_or_stub(self, anime_id: str, players: list[PlayerOption]) -> AnimeCard:
        try:
            return await self.card(anime_id)
        except CatalogError:
            title = players[0].label if players else f"Аниме #{anime_id}"
            return AnimeCard(id=anime_id, title=title, source=self.source_name)

    async def pick_player(
        self, anime_id: str, episode: int, player_key: str | None = None
    ) -> PlayerOption:
        """Выбирает плеер: запрошенный, либо ту же озвучку, либо первый доступный."""
        options = await self.players(anime_id, episode)
        if not options:
            raise NothingFound("Для этой серии не нашлось ни одного плеера.")
        if player_key:
            for option in options:
                if option.key == player_key:
                    return option
            # та же озвучка могла переехать на другой плеер — ищем по названию
            wanted_label = player_key.split("::", 1)[-1]
            for option in options:
                if option.label.lower() == wanted_label:
                    return option
        return options[0]

    async def resolve(self, option: PlayerOption) -> PlayerResult:
        """Прямые ссылки на видео. Результат кэшируется ненадолго: ссылки протухают."""
        key = (option.embed, option.episode)
        return await self._stream_cache.get_or_set(key, lambda: self._extract(option))

    async def _extract(self, option: PlayerOption) -> PlayerResult:
        kwargs: dict[str, Any] = {
            "timeout": self.settings.http_timeout,
            "proxy": self.settings.proxy_or_none,
        }
        # у Kodik-ссылок вида /serial/... номер серии передаётся отдельно
        if "kodik" in option.embed and "/serial/" in option.embed:
            kwargs["episode"] = option.episode
        try:
            result = await extract_async(option.embed, **kwargs)
        except UnsupportedUrl as exc:
            raise CatalogError(
                f"Плеер {option.player} пока не поддерживается.", status=400
            ) from exc
        except NotFound as exc:
            raise NothingFound("Плеер не отдал видео для этой серии.") from exc
        except ServiceError as exc:
            raise CatalogError(
                f"Плеер {option.player} временно недоступен ({exc}). Попробуйте другую озвучку."
            ) from exc
        except AnimeDlCoreError as exc:
            log.warning("Не удалось разобрать плеер %s: %s", option.embed, exc)
            raise CatalogError(
                f"Не удалось получить видео из плеера {option.player}. Попробуйте другую озвучку."
            ) from exc
        if not result.streams:
            raise NothingFound("Плеер не отдал ни одной дорожки.")
        return result

    def close(self) -> None:
        self.source.close()


def _build_source(settings: Settings) -> CatalogSource:
    if settings.catalog_source == "animedia":
        return AnimediaSource(settings)
    return AnimeGoSource(settings)


async def _run(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Запускает блокирующий вызов библиотеки в отдельном потоке."""
    return await anyio.to_thread.run_sync(lambda: func(*args, **kwargs))


async def _guard(awaitable: Any) -> Any:
    """Переводит ошибки библиотеки в понятные пользователю сообщения."""
    try:
        return await awaitable
    except NotFound as exc:
        raise NothingFound(str(exc) or "Ничего не найдено.") from exc
    except ServiceError as exc:
        raise CatalogError(
            "Источник ответил ошибкой — скорее всего сработала защита от ботов. "
            "Помогает зеркало (ANIMEGO_MIRROR), прокси (HTTP_PROXY) "
            "или другой источник (CATALOG_SOURCE)."
        ) from exc
    except AnimeDlCoreError as exc:
        log.warning("Источник каталога сломался: %s", exc)
        raise CatalogError(f"Источник каталога недоступен: {exc}") from exc
