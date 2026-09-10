"""Комнаты совместного просмотра.

Комната — это состояние плеера, общее для всех подключённых зрителей:
что смотрим, играет или на паузе и на какой секунде. Хранится всё в памяти
процесса, поэтому приложение рассчитано на один воркер uvicorn
(см. раздел про масштабирование в README).

Главный принцип синхронизации: источник правды — сервер. Клиент сообщает
«я нажал play на 12.5 секунде», сервер запоминает это вместе с моментом
времени и рассылает остальным. Позиция для опоздавших считается как
``position + (сейчас - момент)``, если воспроизведение идёт.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.auth.session import SessionUser
from app.config import ControlMode, Settings

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from fastapi import WebSocket

log = logging.getLogger(__name__)

#: Без похожих друг на друга символов: код комнаты часто диктуют голосом.
_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_CODE_LENGTH = 7


def generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


class RoomError(Exception):
    """Ошибка, которую можно показать зрителю."""

    def __init__(self, message: str, *, code: str = "room_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class Playback:
    """Состояние воспроизведения."""

    playing: bool = False
    position: float = 0.0
    updated_at: float = field(default_factory=time.time)

    def current(self, *, at: float | None = None) -> float:
        """Позиция «сейчас» с поправкой на прошедшее время."""
        if not self.playing:
            return self.position
        now = at if at is not None else time.time()
        return max(0.0, self.position + (now - self.updated_at))

    def apply(self, *, playing: bool | None = None, position: float | None = None) -> None:
        if position is not None:
            self.position = max(0.0, float(position))
        else:
            self.position = self.current()
        if playing is not None:
            self.playing = playing
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "playing": self.playing,
            "position": round(self.current(), 3),
            "server_time": time.time(),
        }


@dataclass
class Connection:
    """Одно WebSocket-подключение зрителя. Один человек может открыть две вкладки."""

    id: str
    user: SessionUser
    websocket: WebSocket
    joined_at: float = field(default_factory=time.time)

    async def send(self, message: dict[str, Any]) -> bool:
        """Отправляет сообщение. ``False`` — соединение мертво, его надо убрать."""
        try:
            await self.websocket.send_json(message)
            return True
        except Exception:  # разрыв соединения — обычное дело, не шумим в логах
            return False


@dataclass
class ChatMessage:
    id: int
    user_id: int
    user_name: str
    avatar: str | None
    text: str
    created_at: float = field(default_factory=time.time)
    kind: str = "message"
    """``message`` — от зрителя, ``system`` — событие комнаты."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "avatar": self.avatar,
            "text": self.text,
            "created_at": self.created_at,
            "kind": self.kind,
        }


@dataclass
class Room:
    """Комната совместного просмотра."""

    code: str
    name: str
    host_id: int
    host_name: str
    settings: Settings
    is_public: bool = True
    control_mode: ControlMode = "everyone"
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)

    playback: Playback = field(default_factory=Playback)
    source: dict[str, Any] | None = None
    """Снимок :class:`~app.services.catalog.SourceInfo` — то, что смотрит комната."""

    connections: dict[str, Connection] = field(default_factory=dict)
    chat: deque[ChatMessage] = field(default_factory=lambda: deque(maxlen=100))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _chat_counter: int = 0

    # ------------------------------------------------------------- зрители
    @property
    def members(self) -> list[dict[str, Any]]:
        """Уникальные зрители (несколько вкладок одного человека — один зритель)."""
        seen: dict[int, dict[str, Any]] = {}
        for connection in self.connections.values():
            member = seen.setdefault(connection.user.id, connection.user.public())
            member["is_host"] = connection.user.id == self.host_id
            member["tabs"] = member.get("tabs", 0) + 1
        return sorted(seen.values(), key=lambda item: (not item["is_host"], item["name"].lower()))

    @property
    def member_count(self) -> int:
        return len({connection.user.id for connection in self.connections.values()})

    @property
    def is_empty(self) -> bool:
        return not self.connections

    def can_control(self, user: SessionUser) -> bool:
        if self.control_mode == "everyone":
            return True
        return user.id == self.host_id or user.is_admin

    def ensure_can_control(self, user: SessionUser) -> None:
        if not self.can_control(user):
            raise RoomError("Плеером управляет только владелец комнаты.", code="control_denied")

    def ensure_is_host(self, user: SessionUser) -> None:
        if user.id != self.host_id and not user.is_admin:
            raise RoomError("Это может сделать только владелец комнаты.", code="host_only")

    def touch(self) -> None:
        self.last_activity = time.time()

    # -------------------------------------------------------- подключения
    def add_connection(self, connection: Connection) -> None:
        if self.member_count >= self.settings.room_max_members and connection.user.id not in {
            c.user.id for c in self.connections.values()
        }:
            raise RoomError("В комнате уже нет свободных мест.", code="room_full")
        self.connections[connection.id] = connection
        self.touch()

    def remove_connection(self, connection_id: str) -> Connection | None:
        connection = self.connections.pop(connection_id, None)
        self.touch()
        return connection

    def has_user(self, user_id: int) -> bool:
        return any(connection.user.id == user_id for connection in self.connections.values())

    # ------------------------------------------------------------- рассылка
    async def broadcast(self, message: dict[str, Any], *, exclude: str | None = None) -> None:
        targets = [
            connection
            for connection_id, connection in self.connections.items()
            if connection_id != exclude
        ]
        if not targets:
            return
        results = await asyncio.gather(
            *(connection.send(message) for connection in targets), return_exceptions=True
        )
        for connection, ok in zip(targets, results, strict=False):
            if ok is not True:
                self.connections.pop(connection.id, None)

    async def send_system(self, text: str) -> None:
        message = self.add_chat_message(
            user_id=0, user_name="Система", avatar=None, text=text, kind="system"
        )
        await self.broadcast({"type": "chat", "message": message.to_dict()})

    def add_chat_message(
        self, *, user_id: int, user_name: str, avatar: str | None, text: str, kind: str = "message"
    ) -> ChatMessage:
        self._chat_counter += 1
        message = ChatMessage(
            id=self._chat_counter,
            user_id=user_id,
            user_name=user_name,
            avatar=avatar,
            text=text,
            kind=kind,
        )
        self.chat.append(message)
        self.touch()
        return message

    # --------------------------------------------------------------- снимки
    def snapshot(self) -> dict[str, Any]:
        """Полное состояние комнаты — то, что клиент получает при входе."""
        return {
            "code": self.code,
            "name": self.name,
            "host_id": self.host_id,
            "host_name": self.host_name,
            "is_public": self.is_public,
            "control_mode": self.control_mode,
            "created_at": self.created_at,
            "members": self.members,
            "playback": self.playback.to_dict(),
            "source": self.source,
            "chat": [message.to_dict() for message in self.chat],
        }

    def public_info(self) -> dict[str, Any]:
        """Короткая карточка комнаты для списка на главной."""
        source = self.source or {}
        return {
            "code": self.code,
            "name": self.name,
            "host_name": self.host_name,
            "members": self.member_count,
            "max_members": self.settings.room_max_members,
            "control_mode": self.control_mode,
            "anime_title": source.get("anime_title"),
            "episode": source.get("episode"),
            "poster": source.get("poster"),
            "playing": self.playback.playing,
            "created_at": self.created_at,
        }


class RoomManager:
    """Реестр комнат: создание, поиск, фоновая уборка и рассылка синхронизации."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._rooms: dict[str, Room] = {}
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------- CRUD
    def create(
        self,
        *,
        host: SessionUser,
        name: str = "",
        is_public: bool = True,
        control_mode: ControlMode | None = None,
        source: dict[str, Any] | None = None,
    ) -> Room:
        code = generate_code()
        while code in self._rooms:  # pragma: no cover - коллизия крайне маловероятна
            code = generate_code()
        room = Room(
            code=code,
            name=(name or f"Комната {host.display_name}").strip()[:60],
            host_id=host.id,
            host_name=host.display_name,
            settings=self.settings,
            is_public=is_public,
            control_mode=control_mode or self.settings.room_default_control,
            source=source,
        )
        room.chat = deque(maxlen=self.settings.room_chat_history)
        self._rooms[code] = room
        log.info("Создана комната %s (%s) хостом %s", code, room.name, host.display_name)
        return room

    def get(self, code: str) -> Room | None:
        return self._rooms.get(code.strip().lower())

    def require(self, code: str) -> Room:
        room = self.get(code)
        if room is None:
            raise RoomError("Комната не найдена или уже закрылась.", code="room_not_found")
        return room

    def remove(self, code: str) -> None:
        self._rooms.pop(code, None)

    def public_rooms(self) -> list[dict[str, Any]]:
        rooms = [room for room in self._rooms.values() if room.is_public and not room.is_empty]
        rooms.sort(key=lambda room: (-room.member_count, -room.last_activity))
        return [room.public_info() for room in rooms]

    def rooms_of(self, user_id: int) -> list[dict[str, Any]]:
        return [room.public_info() for room in self._rooms.values() if room.has_user(user_id)]

    @property
    def total_rooms(self) -> int:
        return len(self._rooms)

    @property
    def total_viewers(self) -> int:
        return sum(room.member_count for room in self._rooms.values())

    # ------------------------------------------------------- фоновые задачи
    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._sync_loop(), name="rooms-sync"),
            asyncio.create_task(self._cleanup_loop(), name="rooms-cleanup"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def _sync_loop(self) -> None:
        """Периодически рассылает опорное время: так лечится накопленный дрейф."""
        interval = max(2.0, self.settings.room_sync_interval)
        while True:
            await asyncio.sleep(interval)
            for room in list(self._rooms.values()):
                if room.is_empty or not room.playback.playing:
                    continue
                await room.broadcast({"type": "sync", "playback": room.playback.to_dict()})

    async def _cleanup_loop(self) -> None:
        """Убирает комнаты, в которых давно никого нет."""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for code, room in list(self._rooms.items()):
                if not room.is_empty:
                    continue
                if now - room.last_activity > self.settings.room_idle_timeout:
                    self._rooms.pop(code, None)
                    log.info("Комната %s закрыта по простою", code)
