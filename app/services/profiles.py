"""Публичные профили: каталог участников и чужие списки аниме.

Сайт закрыт гильдией, поэтому профиль видно всем, кто вошёл: это тот же
«Мой список», только чужой и без кнопок редактирования. Раздел целиком
выключается настройкой ``PROFILES_ENABLED``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import EpisodeProgress, User, WatchlistEntry

log = logging.getLogger(__name__)

#: Потолок выборки участников: сайт рассчитан на одну гильдию, не на соцсеть.
MAX_USERS = 500


@dataclass(frozen=True)
class ProfileSummary:
    """Карточка участника в каталоге пользователей."""

    id: int
    display_name: str
    username: str
    provider: str
    avatar_url: str | None
    is_admin: bool
    notifications_enabled: bool
    titles: int
    episodes_watched: int
    hours_watched: float
    last_seen_at: Any = None

    @property
    def initial(self) -> str:
        return (self.display_name or "?")[:1].upper()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "username": self.username,
            "provider": self.provider,
            "avatar_url": self.avatar_url,
            "is_admin": self.is_admin,
            "notifications_enabled": self.notifications_enabled,
            "titles": self.titles,
            "episodes_watched": self.episodes_watched,
            "hours_watched": self.hours_watched,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
        }


class ProfileService:
    """Чтение чужих профилей. Ничего не меняет — только выборки."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def directory(
        self, db: AsyncSession, *, query: str = "", limit: int = 200
    ) -> list[ProfileSummary]:
        """Все участники сайта со сводкой просмотра, свежие — первыми."""
        titles = (
            select(WatchlistEntry.user_id.label("user_id"), func.count().label("titles"))
            .group_by(WatchlistEntry.user_id)
            .subquery()
        )
        watched = (
            select(
                EpisodeProgress.user_id.label("user_id"),
                func.count().label("episodes"),
                func.coalesce(func.sum(EpisodeProgress.position), 0.0).label("seconds"),
            )
            .where(EpisodeProgress.completed.is_(True))
            .group_by(EpisodeProgress.user_id)
            .subquery()
        )
        stmt = (
            select(User, titles.c.titles, watched.c.episodes, watched.c.seconds)
            .outerjoin(titles, titles.c.user_id == User.id)
            .outerjoin(watched, watched.c.user_id == User.id)
            .order_by(User.last_seen_at.desc(), User.id.desc())
            .limit(MAX_USERS)
        )
        rows = (await db.execute(stmt)).all()
        # Фильтруем в Python, а не в SQL: LOWER() в sqlite умеет только латиницу,
        # так что «саку» не нашло бы «Сакуру». Участников на сайте немного.
        needle = query.strip().lower()
        if needle:
            rows = [
                row
                for row in rows
                if needle in row[0].display_name.lower() or needle in row[0].username.lower()
            ]
        return [
            ProfileSummary(
                id=user.id,
                display_name=user.display_name,
                username=user.username,
                provider=user.provider,
                avatar_url=user.avatar_url,
                is_admin=user.is_admin,
                notifications_enabled=user.notifications_enabled,
                titles=int(titles_count or 0),
                episodes_watched=int(episodes or 0),
                hours_watched=round(float(seconds or 0.0) / 3600, 1),
                last_seen_at=user.last_seen_at,
            )
            for user, titles_count, episodes, seconds in rows[: max(1, limit)]
        ]

    async def get_user(self, db: AsyncSession, user_id: int) -> User | None:
        return await db.get(User, user_id)

    async def recent(
        self, db: AsyncSession, user_id: int, *, limit: int = 8
    ) -> list[dict[str, Any]]:
        """Последние серии, к которым человек прикасался."""
        stmt = (
            select(EpisodeProgress, WatchlistEntry)
            .join(
                WatchlistEntry,
                (WatchlistEntry.user_id == EpisodeProgress.user_id)
                & (WatchlistEntry.source == EpisodeProgress.source)
                & (WatchlistEntry.anime_id == EpisodeProgress.anime_id),
            )
            .where(EpisodeProgress.user_id == user_id)
            .order_by(EpisodeProgress.updated_at.desc())
            .limit(max(1, min(limit, 50)))
        )
        rows = (await db.execute(stmt)).all()
        return [
            {
                "source": progress.source,
                "anime_id": progress.anime_id,
                "title": entry.title,
                "poster": entry.poster_url,
                "episode": progress.episode,
                "percent": progress.percent,
                "completed": progress.completed,
                "updated_at": progress.updated_at,
            }
            for progress, entry in rows
        ]
