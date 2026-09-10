"""Общие зависимости FastAPI: настройки, текущий пользователь, сервисы приложения."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.discord import DiscordClient
from app.auth.session import SESSION_KEY, SessionUser
from app.config import Settings, get_settings
from app.db.session import get_db
from app.services.catalog import Catalog
from app.services.playback import PlaybackService
from app.services.rooms import RoomManager
from app.services.streaming import StreamProxy
from app.services.tracking import TrackingService


class LoginRequired(Exception):
    """Страница требует входа. Обработчик решит: редирект или 401 JSON."""

    def __init__(self, next_url: str = "/") -> None:
        super().__init__("login required")
        self.next_url = next_url


def get_current_user(request: Request) -> SessionUser | None:
    return SessionUser.from_session(request.session.get(SESSION_KEY))


def require_user(request: Request) -> SessionUser:
    user = get_current_user(request)
    if user is None:
        query = f"?{request.url.query}" if request.url.query else ""
        raise LoginRequired(next_url=f"{request.url.path}{query}")
    return user


def ws_user(websocket: WebSocket) -> SessionUser | None:
    """Пользователь для WebSocket-соединения (SessionMiddleware работает и здесь)."""
    return SessionUser.from_session(websocket.session.get(SESSION_KEY))


def get_discord(request: Request) -> DiscordClient:
    return request.app.state.discord


def get_catalog(request: Request) -> Catalog:
    return request.app.state.catalog


def get_rooms(request: Request) -> RoomManager:
    return request.app.state.rooms


def get_stream_proxy(request: Request) -> StreamProxy:
    return request.app.state.streams


def get_playback(request: Request) -> PlaybackService:
    return request.app.state.playback


def get_tracking(request: Request) -> TrackingService:
    return request.app.state.tracking


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
UserDep = Annotated[SessionUser, Depends(require_user)]
OptionalUserDep = Annotated[SessionUser | None, Depends(get_current_user)]
DiscordDep = Annotated[DiscordClient, Depends(get_discord)]
CatalogDep = Annotated[Catalog, Depends(get_catalog)]
RoomsDep = Annotated[RoomManager, Depends(get_rooms)]
StreamDep = Annotated[StreamProxy, Depends(get_stream_proxy)]
PlaybackDep = Annotated[PlaybackService, Depends(get_playback)]
TrackingDep = Annotated[TrackingService, Depends(get_tracking)]
