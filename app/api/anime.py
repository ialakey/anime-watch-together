"""REST для каталога: поиск, карточка, серии, озвучки."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.deps import CatalogDep, UserDep

router = APIRouter(prefix="/api/anime", tags=["anime"])


@router.get("/search", summary="Поиск аниме")
async def search(
    catalog: CatalogDep,
    user: UserDep,
    q: str = Query(min_length=2, max_length=100, description="Название аниме"),
    limit: int = Query(default=24, ge=1, le=50),
) -> dict[str, Any]:
    results = await catalog.search(q, limit)
    return {
        "query": q,
        "source": catalog.source_name,
        "results": [card.to_dict() for card in results],
    }


@router.get("/{anime_id}", summary="Карточка аниме со списком серий")
async def details(
    anime_id: str,
    catalog: CatalogDep,
    user: UserDep,
    episode: int = Query(default=1, ge=1),
) -> dict[str, Any]:
    result = await catalog.details(anime_id, episode)
    return result.to_dict()


@router.get("/{anime_id}/players", summary="Плееры и озвучки для серии")
async def players(
    anime_id: str,
    catalog: CatalogDep,
    user: UserDep,
    episode: int = Query(default=1, ge=1),
) -> dict[str, Any]:
    options = await catalog.players(anime_id, episode)
    return {
        "anime_id": anime_id,
        "episode": episode,
        "players": [option.to_dict() for option in options],
    }
