"""Страницы сайта (server-side rendering на Jinja2)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db.models import WatchStatus
from app.deps import (
    CatalogDep,
    DbDep,
    OptionalUserDep,
    RoomsDep,
    SettingsDep,
    TrackingDep,
    UserDep,
)
from app.services.catalog import CatalogError
from app.services.tracking import AnimeRef

log = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)
templates: Jinja2Templates | None = None


def setup_templates(instance: Jinja2Templates) -> None:
    global templates
    templates = instance


def render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    assert templates is not None, "Шаблоны не инициализированы"
    return templates.TemplateResponse(request, name, context)


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    rooms: RoomsDep,
    db: DbDep,
    tracking: TrackingDep,
    settings: SettingsDep,
    user: OptionalUserDep,
) -> HTMLResponse:
    if user is None:
        return render(request, "landing.html", {"title": settings.app_name})
    continue_items: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    if settings.tracking_enabled:
        continue_items = await tracking.continue_watching(db, user.id, limit=8)
        stats = await tracking.stats(db, user.id)
    return render(
        request,
        "home.html",
        {
            "title": "Главная",
            "public_rooms": rooms.public_rooms(),
            "my_rooms": rooms.rooms_of(user.id),
            "continue_items": continue_items,
            "stats": stats,
        },
    )


@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(
    request: Request,
    settings: SettingsDep,
    user: OptionalUserDep,
    error: str = "",
    next: str = "/",
) -> HTMLResponse | RedirectResponse:
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return render(
        request,
        "login.html",
        {"title": "Вход", "error": error, "next": next if next.startswith("/") else "/"},
    )


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    catalog: CatalogDep,
    user: UserDep,
    q: str = Query(default="", max_length=100),
) -> HTMLResponse:
    results: list[Any] = []
    error = ""
    query = q.strip()
    if query:
        try:
            results = await catalog.search(query)
        except CatalogError as exc:
            error = exc.message
    return render(
        request,
        "search.html",
        {
            "title": f"Поиск: {query}" if query else "Поиск",
            "query": query,
            "results": results,
            "error": error,
            "source": catalog.source_name,
        },
    )


@router.get("/anime/{anime_id}", response_class=HTMLResponse)
async def anime_page(
    request: Request,
    anime_id: str,
    catalog: CatalogDep,
    db: DbDep,
    tracking: TrackingDep,
    settings: SettingsDep,
    user: UserDep,
    episode: int = Query(default=1, ge=1),
) -> HTMLResponse:
    try:
        details = await catalog.details(anime_id, episode)
    except CatalogError as exc:
        return render(
            request,
            "error.html",
            {"title": "Не получилось", "message": exc.message, "status": exc.status},
        )

    ref = AnimeRef(
        source=catalog.source_name,
        anime_id=anime_id,
        title=details.card.title,
        poster=details.card.poster,
    )
    entry = None
    progress: dict[int, Any] = {}
    if settings.tracking_enabled:
        entry = await tracking.get_entry(db, user.id, ref)
        progress = await tracking.episodes_progress(db, user.id, ref)
    return render(
        request,
        "anime.html",
        {
            "title": details.card.title,
            "anime": details.card,
            "episodes": details.episodes,
            "players": details.players,
            "current_episode": details.episode,
            "entry": entry,
            "progress": progress,
            "statuses": list(WatchStatus),
        },
    )


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    db: DbDep,
    tracking: TrackingDep,
    settings: SettingsDep,
    user: UserDep,
    status: str = Query(default=""),
) -> HTMLResponse:
    if not settings.tracking_enabled:
        return render(
            request,
            "error.html",
            {
                "title": "Трекинг выключен",
                "message": "Администратор отключил трекинг.",
                "status": 503,
            },
        )
    selected = None
    if status:
        try:
            selected = WatchStatus(status)
        except ValueError:
            selected = None
    entries = await tracking.list_entries(db, user.id, selected)
    return render(
        request,
        "library.html",
        {
            "title": "Мой список",
            "entries": entries,
            "counts": await tracking.status_counts(db, user.id),
            "statuses": list(WatchStatus),
            "selected": selected,
            "stats": await tracking.stats(db, user.id),
        },
    )


@router.get("/room/{code}", response_class=HTMLResponse)
async def room_page(
    request: Request,
    code: str,
    rooms: RoomsDep,
    settings: SettingsDep,
    user: UserDep,
) -> HTMLResponse:
    room = rooms.get(code)
    if room is None:
        return render(
            request,
            "error.html",
            {
                "title": "Комната не найдена",
                "message": "Такой комнаты нет — возможно, её уже закрыли. Создайте новую.",
                "status": 404,
            },
        )
    return render(
        request,
        "room.html",
        {
            "title": room.name,
            "room": room,
            "room_snapshot": room.snapshot(),
            "is_host": room.host_id == user.id,
            "client_config": {
                "code": room.code,
                "user": user.public(),
                "sync_tolerance": settings.room_sync_tolerance,
                "progress_interval": settings.progress_report_interval,
                "tracking_enabled": settings.tracking_enabled,
            },
        },
    )
