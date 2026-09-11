"""Точка входа: сборка FastAPI-приложения.

Запуск в разработке::

    uvicorn app.main:app --reload

В контейнере всё то же самое делает ``docker/entrypoint.sh``.
"""

from __future__ import annotations

import logging
import logging.config
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.api import anime, notifications, rooms, stream, tracking, users, ws
from app.auth.discord import DiscordClient
from app.auth.router import router as auth_router
from app.config import Settings, get_settings
from app.db.session import dispose_engine, init_models
from app.deps import LoginRequired, get_current_user
from app.services.catalog import Catalog, CatalogError
from app.services.notifications import NotificationService
from app.services.playback import PlaybackService
from app.services.profiles import ProfileService
from app.services.rooms import RoomError, RoomManager
from app.services.streaming import StreamProxy
from app.services.tracking import TrackingService
from app.web import views

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("app")


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # DEBUG нужен нашему коду, а не тому, как httpx открывает сокеты
    for noisy in ("httpx", "httpcore", "urllib3", "multipart", "python_multipart", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    if settings.is_sqlite:
        # для sqlite миграции не обязательны — поднимаем схему на месте
        await init_models()
    await app.state.rooms.start()
    await app.state.notifications.start()
    log.info(
        "%s запущен: авторизация Discord — %s, источник каталога — %s",
        settings.app_name,
        "включена" if settings.discord_auth_enabled else "выключена (гостевой режим)",
        settings.catalog_source,
    )
    try:
        yield
    finally:
        await app.state.rooms.stop()
        await app.state.notifications.stop()
        await app.state.discord.aclose()
        await app.state.streams.aclose()
        app.state.catalog.close()
        await dispose_engine()
        log.info("Остановлено")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=settings.app_name,
        description="Совместный просмотр аниме с синхронным плеером и трекингом эпизодов.",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # ---- состояние приложения (один набор сервисов на процесс)
    app.state.settings = settings
    app.state.discord = DiscordClient(settings)
    app.state.catalog = Catalog(settings)
    app.state.streams = StreamProxy(settings)
    app.state.rooms = RoomManager(settings)
    app.state.tracking = TrackingService(settings)
    app.state.profiles = ProfileService(settings)
    app.state.notifications = NotificationService(settings, app.state.catalog)
    app.state.playback = PlaybackService(settings, app.state.catalog, app.state.streams)

    # ---- middleware
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="awt_session",
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=settings.base_url.startswith("https://"),
    )

    # ---- шаблоны и статика
    templates = Jinja2Templates(
        directory=str(BASE_DIR / "templates"), context_processors=[_template_context]
    )
    templates.env.globals.update(
        settings=settings,
        app_name=settings.app_name,
        discord_enabled=settings.discord_auth_enabled,
        tracking_enabled=settings.tracking_enabled,
        profiles_enabled=settings.profiles_enabled,
        notifications_enabled=settings.notifications_enabled,
    )
    templates.env.filters["duration"] = _format_duration
    views.setup_templates(templates)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    # ---- маршруты
    app.include_router(auth_router)
    app.include_router(anime.router)
    app.include_router(rooms.router)
    app.include_router(tracking.router)
    app.include_router(users.router)
    app.include_router(notifications.router)
    app.include_router(stream.router)
    app.include_router(ws.router)
    app.include_router(views.router)

    _register_error_handlers(app, templates)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "rooms": app.state.rooms.total_rooms,
            "viewers": app.state.rooms.total_viewers,
            "discord_auth": settings.discord_auth_enabled,
            "catalog": settings.catalog_source,
            "notifications": settings.notifications_enabled,
        }

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return RedirectResponse("/static/img/favicon.svg", status_code=301)

    return app


def _register_error_handlers(app: FastAPI, templates: Jinja2Templates) -> None:
    def wants_html(request: Request) -> bool:
        if request.url.path.startswith(("/api/", "/ws/")):
            return False
        return "text/html" in request.headers.get("accept", "")

    @app.exception_handler(LoginRequired)
    async def on_login_required(request: Request, exc: LoginRequired) -> Response:
        if wants_html(request):
            from urllib.parse import quote

            return RedirectResponse(f"/login?next={quote(exc.next_url)}", status_code=303)
        return JSONResponse(
            {"detail": "Нужно войти на сайт.", "login_url": "/login"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    @app.exception_handler(CatalogError)
    async def on_catalog_error(request: Request, exc: CatalogError) -> Response:
        if wants_html(request):
            return templates.TemplateResponse(
                request,
                "error.html",
                {"title": "Каталог недоступен", "message": exc.message, "status": exc.status},
                status_code=exc.status,
            )
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.exception_handler(RoomError)
    async def on_room_error(request: Request, exc: RoomError) -> Response:
        if wants_html(request):
            return templates.TemplateResponse(
                request,
                "error.html",
                {"title": "Комната", "message": exc.message, "status": 400},
                status_code=400,
            )
        return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=400)

    @app.exception_handler(404)
    async def on_not_found(request: Request, exc: Exception) -> Response:
        if wants_html(request):
            return templates.TemplateResponse(
                request,
                "error.html",
                {
                    "title": "Страница не найдена",
                    "message": "Такой страницы нет. Возможно, ссылка устарела.",
                    "status": 404,
                    "user": get_current_user(request),
                },
                status_code=404,
            )
        return JSONResponse({"detail": "Not found"}, status_code=404)


def _template_context(request: Request) -> dict[str, object]:
    """Что доступно в каждом шаблоне без явной передачи."""
    return {"user": get_current_user(request)}


def _format_duration(seconds: float | int | None) -> str:
    """``3725`` -> ``1:02:05``. Пустое значение превращается в прочерк."""
    if not seconds:
        return "—"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


app = create_app()


__all__ = ["app", "create_app"]
