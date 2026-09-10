"""Трекинг просмотра: личный список тайтлов и прогресс по эпизодам.

Записи появляются сами: как только человек посмотрел кусок серии, тайтл
добавляется в список со статусом «Смотрю», а серия отмечается просмотренной,
когда доиграла до ``EPISODE_COMPLETED_RATIO`` (по умолчанию 85%).
Руками можно поменять статус, оценку и заметку.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import EpisodeProgress, WatchlistEntry, WatchStatus

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnimeRef:
    """Ссылка на тайтл в терминах источника каталога."""

    source: str
    anime_id: str
    title: str = ""
    poster: str | None = None


class TrackingService:
    """Вся работа со списком и прогрессом. Сессию передаёт вызывающий код."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------- список
    async def get_entry(
        self, db: AsyncSession, user_id: int, ref: AnimeRef
    ) -> WatchlistEntry | None:
        result = await db.execute(
            select(WatchlistEntry).where(
                WatchlistEntry.user_id == user_id,
                WatchlistEntry.source == ref.source,
                WatchlistEntry.anime_id == ref.anime_id,
            )
        )
        return result.scalar_one_or_none()

    async def ensure_entry(
        self,
        db: AsyncSession,
        user_id: int,
        ref: AnimeRef,
        *,
        status: WatchStatus | None = None,
        episodes_total: int | None = None,
    ) -> WatchlistEntry:
        """Возвращает запись списка, создавая её при первом просмотре."""
        entry = await self.get_entry(db, user_id, ref)
        if entry is None:
            entry = WatchlistEntry(
                user_id=user_id,
                source=ref.source,
                anime_id=ref.anime_id,
                title=ref.title or f"Аниме #{ref.anime_id}",
                poster_url=ref.poster,
                status=status or WatchStatus.WATCHING,
                # значения по умолчанию проставляются только при INSERT, а запись
                # читают ещё до flush — поэтому задаём их сразу
                last_episode=0,
            )
            db.add(entry)
        else:
            if ref.title:
                entry.title = ref.title
            if ref.poster:
                entry.poster_url = ref.poster
            if status is not None:
                entry.status = status
        if episodes_total:
            entry.episodes_total = episodes_total
        return entry

    async def set_status(
        self, db: AsyncSession, user_id: int, ref: AnimeRef, status: WatchStatus
    ) -> WatchlistEntry:
        entry = await self.ensure_entry(db, user_id, ref, status=status)
        entry.status = status
        await db.commit()
        await db.refresh(entry)
        return entry

    async def set_rating(
        self, db: AsyncSession, user_id: int, ref: AnimeRef, rating: int | None
    ) -> WatchlistEntry:
        if rating is not None and not 1 <= rating <= 10:
            raise ValueError("Оценка должна быть от 1 до 10.")
        entry = await self.ensure_entry(db, user_id, ref)
        entry.rating = rating
        await db.commit()
        await db.refresh(entry)
        return entry

    async def set_note(
        self, db: AsyncSession, user_id: int, ref: AnimeRef, note: str | None
    ) -> WatchlistEntry:
        entry = await self.ensure_entry(db, user_id, ref)
        entry.note = (note or "").strip()[:2000] or None
        await db.commit()
        await db.refresh(entry)
        return entry

    async def remove_entry(self, db: AsyncSession, user_id: int, ref: AnimeRef) -> None:
        """Убирает тайтл из списка вместе со всем его прогрессом."""
        await db.execute(
            delete(WatchlistEntry).where(
                WatchlistEntry.user_id == user_id,
                WatchlistEntry.source == ref.source,
                WatchlistEntry.anime_id == ref.anime_id,
            )
        )
        await db.execute(
            delete(EpisodeProgress).where(
                EpisodeProgress.user_id == user_id,
                EpisodeProgress.source == ref.source,
                EpisodeProgress.anime_id == ref.anime_id,
            )
        )
        await db.commit()

    async def list_entries(
        self, db: AsyncSession, user_id: int, status: WatchStatus | None = None
    ) -> list[WatchlistEntry]:
        query = select(WatchlistEntry).where(WatchlistEntry.user_id == user_id)
        if status is not None:
            query = query.where(WatchlistEntry.status == status)
        query = query.order_by(WatchlistEntry.updated_at.desc())
        return list((await db.execute(query)).scalars())

    async def status_counts(self, db: AsyncSession, user_id: int) -> dict[str, int]:
        result = await db.execute(
            select(WatchlistEntry.status, func.count())
            .where(WatchlistEntry.user_id == user_id)
            .group_by(WatchlistEntry.status)
        )
        counts = {status.value: 0 for status in WatchStatus}
        for status, count in result.all():
            key = status.value if isinstance(status, WatchStatus) else str(status)
            counts[key] = count
        counts["all"] = sum(counts[status.value] for status in WatchStatus)
        return counts

    # ------------------------------------------------------------ прогресс
    async def get_progress(
        self, db: AsyncSession, user_id: int, ref: AnimeRef, episode: int
    ) -> EpisodeProgress | None:
        result = await db.execute(
            select(EpisodeProgress).where(
                EpisodeProgress.user_id == user_id,
                EpisodeProgress.source == ref.source,
                EpisodeProgress.anime_id == ref.anime_id,
                EpisodeProgress.episode == episode,
            )
        )
        return result.scalar_one_or_none()

    async def episodes_progress(
        self, db: AsyncSession, user_id: int, ref: AnimeRef
    ) -> dict[int, EpisodeProgress]:
        """Прогресс по всем сериям тайтла: ``{номер серии: запись}``."""
        result = await db.execute(
            select(EpisodeProgress).where(
                EpisodeProgress.user_id == user_id,
                EpisodeProgress.source == ref.source,
                EpisodeProgress.anime_id == ref.anime_id,
            )
        )
        return {row.episode: row for row in result.scalars()}

    async def record_progress(
        self,
        db: AsyncSession,
        user_id: int,
        ref: AnimeRef,
        *,
        episode: int,
        position: float,
        duration: float | None = None,
        translation: str | None = None,
        force_completed: bool = False,
    ) -> EpisodeProgress:
        """Сохраняет позицию и, если серия досмотрена, двигает счётчик в списке."""
        if not self.settings.tracking_enabled:
            raise RuntimeError("Трекинг выключен настройкой TRACKING_ENABLED.")

        progress = await self.get_progress(db, user_id, ref, episode)
        if progress is None:
            progress = EpisodeProgress(
                user_id=user_id,
                source=ref.source,
                anime_id=ref.anime_id,
                episode=episode,
                position=0.0,
                completed=False,
            )
            db.add(progress)

        progress.position = max(0.0, float(position))
        if duration and duration > 0:
            progress.duration = float(duration)
        if translation:
            progress.translation = translation[:128]

        completed = force_completed or self._is_completed(progress)
        if completed:
            progress.completed = True

        entry = await self.ensure_entry(db, user_id, ref)
        if completed and episode > entry.last_episode:
            entry.last_episode = episode
        if entry.status in (WatchStatus.PLANNED, WatchStatus.ON_HOLD):
            entry.status = WatchStatus.WATCHING
        if (
            completed
            and entry.episodes_total
            and entry.last_episode >= entry.episodes_total
            and entry.status == WatchStatus.WATCHING
        ):
            entry.status = WatchStatus.COMPLETED

        await db.commit()
        await db.refresh(progress)
        return progress

    def _is_completed(self, progress: EpisodeProgress) -> bool:
        if progress.completed:
            return True
        if not progress.duration:
            return False
        return progress.position / progress.duration >= self.settings.episode_completed_ratio

    async def continue_watching(
        self, db: AsyncSession, user_id: int, limit: int = 12
    ) -> list[dict[str, Any]]:
        """Недосмотренные серии — то, что показываем на главной."""
        query = (
            select(EpisodeProgress, WatchlistEntry)
            .join(
                WatchlistEntry,
                (WatchlistEntry.user_id == EpisodeProgress.user_id)
                & (WatchlistEntry.source == EpisodeProgress.source)
                & (WatchlistEntry.anime_id == EpisodeProgress.anime_id),
            )
            .where(
                EpisodeProgress.user_id == user_id,
                EpisodeProgress.completed.is_(False),
                EpisodeProgress.position > 30,
                WatchlistEntry.status.notin_([WatchStatus.DROPPED, WatchStatus.COMPLETED]),
            )
            .order_by(EpisodeProgress.updated_at.desc())
            .limit(limit)
        )
        rows = (await db.execute(query)).all()
        return [
            {
                "source": progress.source,
                "anime_id": progress.anime_id,
                "title": entry.title,
                "poster": entry.poster_url,
                "episode": progress.episode,
                "position": progress.position,
                "duration": progress.duration,
                "percent": progress.percent,
                "translation": progress.translation,
                "updated_at": progress.updated_at,
            }
            for progress, entry in rows
        ]

    async def watched_episodes(self, db: AsyncSession, user_id: int, ref: AnimeRef) -> set[int]:
        result = await db.execute(
            select(EpisodeProgress.episode).where(
                EpisodeProgress.user_id == user_id,
                EpisodeProgress.source == ref.source,
                EpisodeProgress.anime_id == ref.anime_id,
                EpisodeProgress.completed.is_(True),
            )
        )
        return set(result.scalars())

    async def stats(self, db: AsyncSession, user_id: int) -> dict[str, Any]:
        """Сводка для профиля: сколько серий и часов просмотрено."""
        result = await db.execute(
            select(
                func.count(EpisodeProgress.id),
                func.coalesce(func.sum(EpisodeProgress.position), 0.0),
            ).where(EpisodeProgress.user_id == user_id, EpisodeProgress.completed.is_(True))
        )
        episodes, seconds = result.one()
        titles = await db.scalar(
            select(func.count(WatchlistEntry.id)).where(WatchlistEntry.user_id == user_id)
        )
        return {
            "episodes_watched": int(episodes or 0),
            "hours_watched": round(float(seconds or 0.0) / 3600, 1),
            "titles": int(titles or 0),
        }
