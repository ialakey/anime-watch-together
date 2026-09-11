"""REST для профилей: каталог участников и чужие списки аниме."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app.config import Settings
from app.db.models import WatchStatus
from app.deps import DbDep, ProfilesDep, SettingsDep, TrackingDep, UserDep

router = APIRouter(prefix="/api/users", tags=["users"])


def _ensure_enabled(settings: Settings) -> None:
    if not settings.profiles_enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Профили участников выключены настройкой PROFILES_ENABLED."
        )


@router.get("", summary="Участники сайта")
async def directory(
    db: DbDep,
    profiles: ProfilesDep,
    user: UserDep,
    settings: SettingsDep,
    q: str = Query(default="", max_length=64, description="Фильтр по имени"),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    _ensure_enabled(settings)
    people = await profiles.directory(db, query=q, limit=limit)
    return {"users": [person.to_dict() for person in people]}


@router.get("/{user_id}", summary="Профиль участника")
async def profile(
    user_id: int,
    db: DbDep,
    profiles: ProfilesDep,
    tracking: TrackingDep,
    user: UserDep,
    settings: SettingsDep,
    watch_status: WatchStatus | None = Query(default=None, alias="status"),
) -> dict[str, Any]:
    _ensure_enabled(settings)
    target = await profiles.get_user(db, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Такого участника нет.")
    entries = await tracking.list_entries(db, target.id, watch_status)
    return {
        "user": {
            "id": target.id,
            "display_name": target.display_name,
            "username": target.username,
            "provider": target.provider,
            "avatar_url": target.avatar_url,
            "is_admin": target.is_admin,
        },
        "stats": await tracking.stats(db, target.id),
        "counts": await tracking.status_counts(db, target.id),
        "entries": [
            {
                "source": entry.source,
                "anime_id": entry.anime_id,
                "title": entry.title,
                "poster": entry.poster_url,
                "status": entry.status.value,
                "rating": entry.rating,
                "episodes_total": entry.episodes_total,
                "last_episode": entry.last_episode,
            }
            for entry in entries
        ],
        "recent": await profiles.recent(db, target.id),
    }
