"""Уведомления о новых сериях через Discord-бота.

Как это устроено:

1. Человек подписывается на тайтл (сам — со страницы аниме или из раздела
   «Уведомления»; администратор может подписать кого угодно).
2. В момент подписки запоминается текущий номер последней серии — это база.
   За старые серии уведомлений не приходит.
3. Фоновая задача раз в ``NOTIFY_POLL_INTERVAL`` спрашивает у каталога список
   серий каждого отслеживаемого тайтла. Если максимум вырос — бот пишет
   подписчикам: в личные сообщения либо в канал (``NOTIFY_CHANNEL_ID`` или
   персональный ``notify_channel_id``).

Своего gateway-соединения бот не держит: хватает REST API, тех же вызовов,
что и в :mod:`app.auth.discord`. Поэтому лишних зависимостей нет.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import NotificationSubscription, User, WatchlistEntry, utcnow
from app.db.session import session_scope
from app.services.catalog import Catalog, CatalogError
from app.services.tracking import AnimeRef

log = logging.getLogger(__name__)

#: Цвет полоски эмбеда — тот же акцент, что и на сайте.
EMBED_COLOR = 0xA06BFF

#: Пауза между сообщениями, чтобы не поймать общий rate limit Discord.
SEND_DELAY = 0.4


@dataclass(frozen=True)
class Recipient:
    """Куда именно писать конкретному человеку."""

    kind: str
    """``dm`` — в личные сообщения, ``channel`` — в канал."""
    target: str
    """Discord ID пользователя либо ID канала."""
    mention: str | None = None
    """Упоминание, которое имеет смысл добавить в канале."""


def resolve_recipient(user: User, settings: Settings) -> Recipient | None:
    """Персональный канал важнее общего, общий — важнее личных сообщений."""
    channel = (user.notify_channel_id or settings.notify_channel_id or "").strip()
    mention = f"<@{user.external_id}>" if user.provider == "discord" else None
    if channel:
        return Recipient(kind="channel", target=channel, mention=mention)
    if user.provider == "discord" and user.external_id:
        return Recipient(kind="dm", target=user.external_id)
    return None


class DiscordNotifier:
    """Отправка сообщений ботом. Живёт одна штука на приложение."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": f"{settings.app_name} (+{settings.base_url})"},
        )
        self._own_client = client is None
        #: Discord ID -> ID личного канала. Канал вечный, так что кэш не протухает.
        self._dm_channels: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.notifications_enabled and self.settings.discord_bot_token)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self.settings.discord_bot_token}"}

    async def send(self, recipient: Recipient, payload: dict[str, Any]) -> bool:
        """``True`` — сообщение ушло. Ошибки не бросаем: рассылка не должна падать."""
        if not self.enabled:
            return False
        try:
            if recipient.kind == "dm":
                channel_id = await self._dm_channel(recipient.target)
                if channel_id is None:
                    return False
            else:
                channel_id = recipient.target
            body = dict(payload)
            if recipient.kind == "channel" and recipient.mention:
                body["content"] = f"{recipient.mention} {body.get('content') or ''}".strip()
            return await self._post_message(channel_id, body)
        except httpx.HTTPError as exc:
            log.warning("Бот не смог отправить уведомление: %s", exc)
            return False

    async def _dm_channel(self, discord_id: str) -> str | None:
        cached = self._dm_channels.get(discord_id)
        if cached:
            return cached
        response = await self._client.post(
            f"{self.settings.discord_api_base}/users/@me/channels",
            headers=self._headers,
            json={"recipient_id": discord_id},
        )
        if response.status_code in (200, 201):
            channel_id = str(response.json().get("id", ""))
            if channel_id:
                self._dm_channels[discord_id] = channel_id
                return channel_id
        if response.status_code == 403:
            log.info(
                "Discord не даёт открыть личный канал с %s — скорее всего у человека "
                "закрыты сообщения от участников сервера",
                discord_id,
            )
        else:
            log.warning(
                "Не удалось открыть личный канал с %s: %s %s",
                discord_id,
                response.status_code,
                response.text[:200],
            )
        return None

    async def _post_message(self, channel_id: str, payload: dict[str, Any]) -> bool:
        url = f"{self.settings.discord_api_base}/channels/{channel_id}/messages"
        response = await self._client.post(url, headers=self._headers, json=payload)
        if response.status_code == 429:
            # Discord сам говорит, сколько подождать. Одной повторной попытки хватает.
            retry_after = _retry_after(response)
            log.info("Discord просит подождать %.1f с перед отправкой", retry_after)
            await asyncio.sleep(retry_after)
            response = await self._client.post(url, headers=self._headers, json=payload)
        if response.status_code in (200, 201):
            return True
        if response.status_code in (401, 403):
            log.warning(
                "Бот не имеет права писать в канал %s (%s). Проверьте DISCORD_BOT_TOKEN "
                "и права бота на сервере.",
                channel_id,
                response.status_code,
            )
        else:
            log.warning(
                "Discord отклонил сообщение в %s: %s %s",
                channel_id,
                response.status_code,
                response.text[:200],
            )
        return False

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()


def _retry_after(response: httpx.Response) -> float:
    with contextlib.suppress(Exception):
        return min(30.0, float(response.json().get("retry_after", 1.0)))
    return 1.0


class NotificationService:
    """Подписки на тайтлы и фоновая проверка новых серий."""

    def __init__(
        self, settings: Settings, catalog: Catalog, notifier: DiscordNotifier | None = None
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.notifier = notifier or DiscordNotifier(settings)
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.notifications_enabled

    # ------------------------------------------------------------ подписки
    async def list_subscriptions(
        self, db: AsyncSession, user_id: int
    ) -> list[NotificationSubscription]:
        result = await db.execute(
            select(NotificationSubscription)
            .where(NotificationSubscription.user_id == user_id)
            .order_by(NotificationSubscription.title)
        )
        return list(result.scalars())

    async def get_subscription(
        self, db: AsyncSession, user_id: int, ref: AnimeRef
    ) -> NotificationSubscription | None:
        result = await db.execute(
            select(NotificationSubscription).where(
                NotificationSubscription.user_id == user_id,
                NotificationSubscription.source == ref.source,
                NotificationSubscription.anime_id == ref.anime_id,
            )
        )
        return result.scalar_one_or_none()

    async def subscribe(
        self, db: AsyncSession, user_id: int, ref: AnimeRef
    ) -> NotificationSubscription:
        """Подписывает на тайтл. Базой берётся последняя вышедшая серия."""
        subscription = await self.get_subscription(db, user_id, ref)
        if subscription is None:
            subscription = NotificationSubscription(
                user_id=user_id,
                source=ref.source,
                anime_id=ref.anime_id,
                title=ref.title or f"Аниме #{ref.anime_id}",
                poster_url=ref.poster,
                last_known_episode=await self._latest_episode(ref),
            )
            db.add(subscription)
        else:
            if ref.title:
                subscription.title = ref.title
            if ref.poster:
                subscription.poster_url = ref.poster
        await db.commit()
        await db.refresh(subscription)
        return subscription

    async def unsubscribe(self, db: AsyncSession, user_id: int, ref: AnimeRef) -> None:
        await db.execute(
            delete(NotificationSubscription).where(
                NotificationSubscription.user_id == user_id,
                NotificationSubscription.source == ref.source,
                NotificationSubscription.anime_id == ref.anime_id,
            )
        )
        await db.commit()

    async def subscribed_ids(self, db: AsyncSession, user_id: int) -> set[str]:
        """Идентификаторы тайтлов, на которые человек уже подписан."""
        result = await db.execute(
            select(NotificationSubscription.anime_id).where(
                NotificationSubscription.user_id == user_id
            )
        )
        return set(result.scalars())

    async def subscriber_counts(self, db: AsyncSession) -> dict[int, int]:
        """``{user_id: сколько тайтлов отслеживает}`` — для админского списка."""
        result = await db.execute(
            select(NotificationSubscription.user_id, func.count()).group_by(
                NotificationSubscription.user_id
            )
        )
        return {int(user_id): int(count) for user_id, count in result.all()}

    async def known_titles(self, db: AsyncSession) -> list[dict[str, Any]]:
        """Тайтлы, которые вообще фигурируют на сайте — выбор для админской рассылки."""
        titles: dict[tuple[str, str], dict[str, Any]] = {}
        for model in (WatchlistEntry, NotificationSubscription):
            result = await db.execute(
                select(
                    model.source,
                    model.anime_id,
                    func.min(model.title),
                    func.min(model.poster_url),
                ).group_by(model.source, model.anime_id)
            )
            for source, anime_id, title, poster in result.all():
                titles.setdefault(
                    (source, anime_id),
                    {"source": source, "anime_id": anime_id, "title": title, "poster": poster},
                )
        return sorted(titles.values(), key=lambda item: (item["title"] or "").lower())

    async def set_user_settings(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        enabled: bool | None = None,
        channel_id: str | None = None,
    ) -> User:
        user = await db.get(User, user_id)
        if user is None:
            raise ValueError("Пользователь не найден.")
        if enabled is not None:
            user.notifications_enabled = enabled
        if channel_id is not None:
            cleaned = channel_id.strip()[:32]
            if cleaned and not cleaned.isdigit():
                raise ValueError("ID канала Discord — это число.")
            user.notify_channel_id = cleaned or None
        await db.commit()
        await db.refresh(user)
        return user

    async def _latest_episode(self, ref: AnimeRef) -> int:
        """Номер последней вышедшей серии. При сбое источника — 0 (база снимется позже)."""
        if ref.source != self.catalog.source_name:
            return 0
        try:
            episodes = await self.catalog.episodes(ref.anime_id)
        except CatalogError as exc:
            log.info("Не удалось снять базу по %s: %s", ref.anime_id, exc.message)
            return 0
        return max(episodes, default=0)

    # ------------------------------------------------------------ рассылка
    async def check_once(self, db: AsyncSession) -> int:
        """Один проход по отслеживаемым тайтлам. Возвращает число отправленных сообщений."""
        if not self.enabled:
            return 0
        sent = 0
        for anime_id in await self._watched_titles(db):
            sent += await self._check_title(db, anime_id)
        if sent:
            log.info("Уведомлений о новых сериях отправлено: %s", sent)
        return sent

    async def _watched_titles(self, db: AsyncSession) -> list[str]:
        """Тайтлы текущего источника, у которых есть хотя бы один живой подписчик."""
        result = await db.execute(
            select(NotificationSubscription.anime_id)
            .join(User, User.id == NotificationSubscription.user_id)
            .where(
                User.notifications_enabled.is_(True),
                NotificationSubscription.source == self.catalog.source_name,
            )
            .group_by(NotificationSubscription.anime_id)
        )
        return list(result.scalars())

    async def _check_title(self, db: AsyncSession, anime_id: str) -> int:
        try:
            episodes = await self.catalog.episodes(anime_id)
        except CatalogError as exc:
            log.info("Каталог не отдал серии %s: %s", anime_id, exc.message)
            return 0
        latest = max(episodes, default=0)
        if not latest:
            return 0

        rows = (
            await db.execute(
                select(NotificationSubscription, User)
                .join(User, User.id == NotificationSubscription.user_id)
                .where(
                    NotificationSubscription.source == self.catalog.source_name,
                    NotificationSubscription.anime_id == anime_id,
                    NotificationSubscription.last_known_episode < latest,
                    User.notifications_enabled.is_(True),
                )
            )
        ).all()

        sent = 0
        for subscription, user in rows:
            previous = subscription.last_known_episode
            subscription.last_known_episode = latest
            if not previous:
                # первая проверка после подписки — просто запоминаем, где мы находимся
                continue
            if await self._deliver(subscription, user, latest):
                subscription.last_notified_at = utcnow()
                sent += 1
                await asyncio.sleep(SEND_DELAY)
        await db.commit()
        return sent

    async def _deliver(
        self, subscription: NotificationSubscription, user: User, episode: int
    ) -> bool:
        recipient = resolve_recipient(user, self.settings)
        if recipient is None:
            log.debug("Уведомлять %s некуда: нет ни Discord, ни канала", user.display_name)
            return False
        return await self.notifier.send(recipient, self.episode_message(subscription, episode))

    def episode_message(
        self, subscription: NotificationSubscription, episode: int
    ) -> dict[str, Any]:
        """Сообщение о новой серии."""
        url = f"{self.settings.base_url}/anime/{subscription.anime_id}?episode={episode}"
        embed: dict[str, Any] = {
            "title": subscription.title,
            "url": url,
            "description": f"Вышла **{episode}** серия — можно смотреть.",
            "color": EMBED_COLOR,
            "footer": {"text": self.settings.app_name},
        }
        if subscription.poster_url:
            embed["thumbnail"] = {"url": subscription.poster_url}
        return {"embeds": [embed]}

    async def send_test(self, db: AsyncSession, user_id: int) -> bool:
        """Проверочное сообщение — чтобы человек убедился, что бот до него достучится."""
        user = await db.get(User, user_id)
        if user is None:
            raise ValueError("Пользователь не найден.")
        recipient = resolve_recipient(user, self.settings)
        if recipient is None:
            raise ValueError(
                "Писать некуда: войдите через Discord или укажите ID канала для уведомлений."
            )
        payload = {
            "embeds": [
                {
                    "title": "Уведомления работают",
                    "description": (
                        "Так будет выглядеть сообщение о новой серии на сайте "
                        f"[{self.settings.app_name}]({self.settings.base_url})."
                    ),
                    "color": EMBED_COLOR,
                    "footer": {"text": self.settings.app_name},
                }
            ]
        }
        return await self.notifier.send(recipient, payload)

    # ------------------------------------------------------- фоновая задача
    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="notifications-watch")
        log.info(
            "Уведомления о новых сериях включены: проверка раз в %.0f мин",
            self.settings.notify_interval / 60,
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.notifier.aclose()

    async def _loop(self) -> None:
        interval = self.settings.notify_interval
        while True:
            await asyncio.sleep(interval)
            try:
                async with session_scope() as db:
                    await self.check_once(db)
            except asyncio.CancelledError:
                raise
            except Exception:  # одна неудачная проверка не должна убивать задачу
                log.exception("Проверка новых серий сорвалась")


def subscription_to_dict(subscription: NotificationSubscription) -> dict[str, Any]:
    last_notified: datetime | None = subscription.last_notified_at
    return {
        "source": subscription.source,
        "anime_id": subscription.anime_id,
        "title": subscription.title,
        "poster": subscription.poster_url,
        "last_known_episode": subscription.last_known_episode,
        "last_notified_at": last_notified.isoformat() if last_notified else None,
    }
