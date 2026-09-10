"""WebSocket комнаты: синхронизация плеера, чат и смена серий.

Формат сообщений — JSON с полем ``type``.

От клиента:
    ``play`` / ``pause`` / ``seek``   — управление плеером (``position`` в секундах)
    ``sync_request``                  — «где мы сейчас?» после буферизации
    ``set_source``                    — включить другую серию или озвучку
    ``progress``                      — позиция просмотра для трекинга
    ``chat``                          — сообщение в чат
    ``settings``                      — настройки комнаты (только владелец)
    ``ping``                          — замер задержки

От сервера:
    ``state``     — полный снимок комнаты (сразу после входа)
    ``playback``  — кто-то нажал play/pause/перемотал
    ``sync``      — опорное время, рассылается периодически
    ``source``    — комната переключилась на другую серию
    ``members``   — состав зрителей изменился
    ``chat``      — новое сообщение
    ``settings``  — настройки комнаты изменились
    ``error``     — что-то пошло не так (комната живёт дальше)
    ``closed``    — комната закрыта, соединение сейчас разорвётся
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.auth.session import SessionUser
from app.config import Settings
from app.db.session import session_scope
from app.deps import ws_user
from app.services.catalog import CatalogError
from app.services.playback import PlaybackService
from app.services.rooms import Connection, Playback, Room, RoomError, RoomManager
from app.services.tracking import AnimeRef, TrackingService

log = logging.getLogger(__name__)

router = APIRouter()

WS_UNAUTHORIZED = 4401
WS_NOT_FOUND = 4404
WS_ROOM_FULL = 4409

#: Не даём заспамить комнату: не больше N событий за окно.
_CHAT_LIMIT = (6, 5.0)
_CONTROL_LIMIT = (25, 5.0)


class RateLimiter:
    """Примитивное скользящее окно на одно соединение."""

    def __init__(self) -> None:
        self._events: dict[str, list[float]] = {}

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        events = [moment for moment in self._events.get(key, []) if now - moment < window]
        if len(events) >= limit:
            self._events[key] = events
            return False
        events.append(now)
        self._events[key] = events
        return True


@router.websocket("/ws/room/{code}")
async def room_socket(websocket: WebSocket, code: str) -> None:
    user = ws_user(websocket)
    if user is None:
        await websocket.close(code=WS_UNAUTHORIZED, reason="Нужно войти на сайт")
        return

    rooms: RoomManager = websocket.app.state.rooms
    room = rooms.get(code)
    if room is None:
        await websocket.close(code=WS_NOT_FOUND, reason="Комната не найдена")
        return

    connection = Connection(id=uuid.uuid4().hex, user=user, websocket=websocket)
    await websocket.accept()
    try:
        room.add_connection(connection)
    except RoomError as exc:
        await websocket.close(code=WS_ROOM_FULL, reason=exc.message)
        return

    session = RoomSession(
        websocket=websocket,
        room=room,
        connection=connection,
        user=user,
        settings=websocket.app.state.settings,
        playback=websocket.app.state.playback,
        tracking=websocket.app.state.tracking,
    )
    try:
        await session.run()
    finally:
        await session.leave()


class RoomSession:
    """Обслуживает одно подключение к комнате."""

    def __init__(
        self,
        *,
        websocket: WebSocket,
        room: Room,
        connection: Connection,
        user: SessionUser,
        settings: Settings,
        playback: PlaybackService,
        tracking: TrackingService,
    ) -> None:
        self.websocket = websocket
        self.room = room
        self.connection = connection
        self.user = user
        self.settings = settings
        self.playback = playback
        self.tracking = tracking
        self.limiter = RateLimiter()

    # ------------------------------------------------------------ жизненный цикл
    async def run(self) -> None:
        await self.send({"type": "state", "room": self.room.snapshot(), "you": self.user.public()})
        await self.broadcast_members()
        if self.room.member_count > 1 or self.room.host_id != self.user.id:
            await self.room.send_system(f"{self.user.display_name} присоединился к просмотру")

        while True:
            try:
                message = await self.websocket.receive_json()
            except WebSocketDisconnect:
                return
            except ValueError:
                await self.send_error("bad_message", "Сообщение не разобрано как JSON.")
                continue
            if not isinstance(message, dict):
                await self.send_error("bad_message", "Ожидался JSON-объект.")
                continue
            try:
                await self.dispatch(message)
            except RoomError as exc:
                await self.send_error(exc.code, exc.message)
            except CatalogError as exc:
                await self.send_error("catalog_error", exc.message)
            except Exception:  # одна кривая команда не должна ронять комнату
                log.exception("Ошибка обработки сообщения комнаты %s", self.room.code)
                await self.send_error("internal", "Внутренняя ошибка, попробуйте ещё раз.")

    async def leave(self) -> None:
        self.room.remove_connection(self.connection.id)
        if not self.room.has_user(self.user.id):
            await self.room.send_system(f"{self.user.display_name} вышел")
        await self.broadcast_members()

    # ------------------------------------------------------------- диспетчер
    async def dispatch(self, message: dict[str, Any]) -> None:
        handlers = {
            "play": self.on_play,
            "pause": self.on_pause,
            "seek": self.on_seek,
            "sync_request": self.on_sync_request,
            "set_source": self.on_set_source,
            "progress": self.on_progress,
            "chat": self.on_chat,
            "settings": self.on_settings,
            "ping": self.on_ping,
        }
        handler = handlers.get(str(message.get("type", "")))
        if handler is None:
            await self.send_error("unknown_type", f"Неизвестная команда: {message.get('type')!r}")
            return
        await handler(message)

    # ------------------------------------------------------------- плеер
    async def on_play(self, message: dict[str, Any]) -> None:
        await self._apply_playback(message, playing=True, action="play")

    async def on_pause(self, message: dict[str, Any]) -> None:
        await self._apply_playback(message, playing=False, action="pause")

    async def on_seek(self, message: dict[str, Any]) -> None:
        await self._apply_playback(message, playing=None, action="seek")

    async def _apply_playback(
        self, message: dict[str, Any], *, playing: bool | None, action: str
    ) -> None:
        self.room.ensure_can_control(self.user)
        if not self.limiter.allow("control", *_CONTROL_LIMIT):
            return  # молча гасим шторм событий от одного клиента
        position = _as_float(message.get("position"))
        async with self.room.lock:
            self.room.playback.apply(playing=playing, position=position)
            self.room.touch()
            payload = {
                "type": "playback",
                "action": action,
                "by": self.user.display_name,
                "by_id": self.user.id,
                "playback": self.room.playback.to_dict(),
            }
            await self.room.broadcast(payload, exclude=self.connection.id)
        await self.send({"type": "ack", "action": action, "playback": self.room.playback.to_dict()})

    async def on_sync_request(self, _message: dict[str, Any]) -> None:
        await self.send({"type": "sync", "playback": self.room.playback.to_dict()})

    # -------------------------------------------------------------- источник
    async def on_set_source(self, message: dict[str, Any]) -> None:
        self.room.ensure_can_control(self.user)
        current = self.room.source or {}
        anime_id = str(message.get("anime_id") or current.get("anime_id") or "")
        if not anime_id:
            raise RoomError("Не указано, что включать.", code="no_anime")
        episode = int(_as_float(message.get("episode")) or current.get("episode") or 1)
        player_key = message.get("player_key")
        if player_key is None and str(anime_id) == str(current.get("anime_id")):
            player_key = current.get("player_key")  # держимся той же озвучки между сериями

        await self.send({"type": "loading", "episode": episode})
        info = await self.playback.build_source(anime_id, max(1, episode), player_key=player_key)
        source = info.to_dict()
        if current.get("poster") and not source.get("poster"):
            source["poster"] = current["poster"]
        if current.get("anime_title") and source["anime_title"].startswith("Аниме #"):
            source["anime_title"] = current["anime_title"]

        async with self.room.lock:
            self.room.source = source
            self.room.playback = Playback(playing=False, position=0.0)
            self.room.touch()
            await self.room.broadcast(
                {
                    "type": "source",
                    "source": source,
                    "playback": self.room.playback.to_dict(),
                    "by": self.user.display_name,
                }
            )
        await self.room.send_system(
            f"{self.user.display_name} включил серию {source['episode']} ({source['translation']})"
        )

    # -------------------------------------------------------------- трекинг
    async def on_progress(self, message: dict[str, Any]) -> None:
        if not self.settings.tracking_enabled:
            return
        source = self.room.source
        if not source:
            return
        position = _as_float(message.get("position")) or 0.0
        duration = _as_float(message.get("duration"))
        ref = AnimeRef(
            source=self.settings.catalog_source,
            anime_id=str(source["anime_id"]),
            title=str(source.get("anime_title") or ""),
            poster=source.get("poster"),
        )
        async with session_scope() as db:
            await self.tracking.record_progress(
                db,
                self.user.id,
                ref,
                episode=int(source["episode"]),
                position=position,
                duration=duration,
                translation=source.get("translation"),
            )

    # ------------------------------------------------------------------ чат
    async def on_chat(self, message: dict[str, Any]) -> None:
        text = str(message.get("text", "")).strip()[:500]
        if not text:
            return
        if not self.limiter.allow("chat", *_CHAT_LIMIT):
            await self.send_error("rate_limited", "Слишком часто. Немного подождите.")
            return
        chat_message = self.room.add_chat_message(
            user_id=self.user.id,
            user_name=self.user.display_name,
            avatar=self.user.avatar_url,
            text=text,
        )
        await self.room.broadcast({"type": "chat", "message": chat_message.to_dict()})

    # ------------------------------------------------------------ настройки
    async def on_settings(self, message: dict[str, Any]) -> None:
        self.room.ensure_is_host(self.user)
        changed: dict[str, Any] = {}
        control_mode = message.get("control_mode")
        if control_mode in ("everyone", "host"):
            self.room.control_mode = control_mode
            changed["control_mode"] = control_mode
        if "is_public" in message:
            self.room.is_public = bool(message["is_public"])
            changed["is_public"] = self.room.is_public
        name = str(message.get("name", "")).strip()[:60]
        if name:
            self.room.name = name
            changed["name"] = name
        if not changed:
            return
        self.room.touch()
        await self.room.broadcast({"type": "settings", "settings": changed})
        if "control_mode" in changed:
            await self.room.send_system(
                "Плеером теперь управляют все"
                if changed["control_mode"] == "everyone"
                else "Плеером теперь управляет только владелец"
            )

    async def on_ping(self, message: dict[str, Any]) -> None:
        await self.send(
            {"type": "pong", "client_time": message.get("client_time"), "server_time": time.time()}
        )

    # -------------------------------------------------------------- отправка
    async def send(self, message: dict[str, Any]) -> None:
        if not await self.connection.send(message):
            raise WebSocketDisconnect(code=1006)

    async def send_error(self, code: str, message: str) -> None:
        await self.connection.send({"type": "error", "code": code, "message": message})

    async def broadcast_members(self) -> None:
        await self.room.broadcast({"type": "members", "members": self.room.members})


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
