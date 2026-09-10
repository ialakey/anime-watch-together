"""REST для трекинга: личный список и прогресс по сериям."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.config import Settings
from app.db.models import WatchStatus
from app.deps import DbDep, SettingsDep, TrackingDep, UserDep
from app.services.tracking import AnimeRef

router = APIRouter(prefix="/api/tracking", tags=["tracking"])


class AnimeRefPayload(BaseModel):
    anime_id: str = Field(max_length=128)
    source: str = Field(default="animego", max_length=16)
    title: str = Field(default="", max_length=255)
    poster: str | None = Field(default=None, max_length=512)

    def to_ref(self) -> AnimeRef:
        return AnimeRef(
            source=self.source, anime_id=self.anime_id, title=self.title, poster=self.poster
        )


class StatusPayload(AnimeRefPayload):
    status: WatchStatus


class RatingPayload(AnimeRefPayload):
    rating: int | None = Field(default=None, ge=1, le=10)


class NotePayload(AnimeRefPayload):
    note: str = Field(default="", max_length=2000)


class ProgressPayload(AnimeRefPayload):
    episode: int = Field(ge=1)
    position: float = Field(ge=0)
    duration: float | None = Field(default=None, ge=0)
    translation: str | None = Field(default=None, max_length=128)
    completed: bool = False


def _entry_to_dict(entry: Any) -> dict[str, Any]:
    return {
        "source": entry.source,
        "anime_id": entry.anime_id,
        "title": entry.title,
        "poster": entry.poster_url,
        "status": entry.status.value if hasattr(entry.status, "value") else entry.status,
        "rating": entry.rating,
        "episodes_total": entry.episodes_total,
        "last_episode": entry.last_episode,
        "note": entry.note,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
    }


def _ensure_enabled(settings: Settings) -> None:
    if not settings.tracking_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Трекинг выключен настройкой TRACKING_ENABLED."
        )


@router.get("/list", summary="Мой список аниме")
async def list_entries(
    db: DbDep,
    tracking: TrackingDep,
    user: UserDep,
    settings: SettingsDep,
    watch_status: WatchStatus | None = Query(default=None, alias="status"),
) -> dict[str, Any]:
    _ensure_enabled(settings)
    entries = await tracking.list_entries(db, user.id, watch_status)
    return {
        "entries": [_entry_to_dict(entry) for entry in entries],
        "counts": await tracking.status_counts(db, user.id),
    }


@router.get("/continue", summary="Продолжить просмотр")
async def continue_watching(
    db: DbDep,
    tracking: TrackingDep,
    user: UserDep,
    settings: SettingsDep,
    limit: int = Query(default=12, ge=1, le=50),
) -> dict[str, Any]:
    _ensure_enabled(settings)
    return {"items": await tracking.continue_watching(db, user.id, limit)}


@router.get("/stats", summary="Сводка просмотра")
async def stats(
    db: DbDep, tracking: TrackingDep, user: UserDep, settings: SettingsDep
) -> dict[str, Any]:
    _ensure_enabled(settings)
    return await tracking.stats(db, user.id)


@router.get("/anime/{anime_id}", summary="Прогресс по сериям тайтла")
async def anime_progress(
    anime_id: str,
    db: DbDep,
    tracking: TrackingDep,
    user: UserDep,
    settings: SettingsDep,
    source: str = Query(default="animego"),
) -> dict[str, Any]:
    _ensure_enabled(settings)
    ref = AnimeRef(source=source, anime_id=anime_id)
    entry = await tracking.get_entry(db, user.id, ref)
    progress = await tracking.episodes_progress(db, user.id, ref)
    return {
        "entry": _entry_to_dict(entry) if entry else None,
        "episodes": {
            str(number): {
                "position": item.position,
                "duration": item.duration,
                "percent": item.percent,
                "completed": item.completed,
                "translation": item.translation,
            }
            for number, item in progress.items()
        },
    }


@router.put("/status", summary="Поменять статус тайтла")
async def set_status(
    payload: StatusPayload,
    db: DbDep,
    tracking: TrackingDep,
    user: UserDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    _ensure_enabled(settings)
    entry = await tracking.set_status(db, user.id, payload.to_ref(), payload.status)
    return _entry_to_dict(entry)


@router.put("/rating", summary="Поставить оценку")
async def set_rating(
    payload: RatingPayload,
    db: DbDep,
    tracking: TrackingDep,
    user: UserDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    _ensure_enabled(settings)
    entry = await tracking.set_rating(db, user.id, payload.to_ref(), payload.rating)
    return _entry_to_dict(entry)


@router.put("/note", summary="Сохранить заметку")
async def set_note(
    payload: NotePayload,
    db: DbDep,
    tracking: TrackingDep,
    user: UserDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    _ensure_enabled(settings)
    entry = await tracking.set_note(db, user.id, payload.to_ref(), payload.note)
    return _entry_to_dict(entry)


@router.put("/progress", summary="Сохранить позицию просмотра")
async def save_progress(
    payload: ProgressPayload,
    db: DbDep,
    tracking: TrackingDep,
    user: UserDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    _ensure_enabled(settings)
    progress = await tracking.record_progress(
        db,
        user.id,
        payload.to_ref(),
        episode=payload.episode,
        position=payload.position,
        duration=payload.duration,
        translation=payload.translation,
        force_completed=payload.completed,
    )
    return {
        "episode": progress.episode,
        "position": progress.position,
        "duration": progress.duration,
        "completed": progress.completed,
        "percent": progress.percent,
    }


@router.delete("/entry", summary="Убрать тайтл из списка")
async def remove_entry(
    payload: AnimeRefPayload,
    db: DbDep,
    tracking: TrackingDep,
    user: UserDep,
    settings: SettingsDep,
) -> dict[str, str]:
    _ensure_enabled(settings)
    await tracking.remove_entry(db, user.id, payload.to_ref())
    return {"status": "removed"}
