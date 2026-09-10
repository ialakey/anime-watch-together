"""REST для комнат: создать, посмотреть список, узнать состояние.

Управление плеером идёт по WebSocket (``app/api/ws.py``) — здесь только то,
что нужно до входа в комнату.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.config import ControlMode
from app.deps import CatalogDep, PlaybackDep, RoomsDep, SettingsDep, UserDep

router = APIRouter(prefix="/api/rooms", tags=["rooms"])


class CreateRoomRequest(BaseModel):
    name: str = Field(default="", max_length=60)
    is_public: bool = True
    control_mode: ControlMode | None = None
    anime_id: str | None = Field(default=None, description="Что включить сразу после создания")
    episode: int = Field(default=1, ge=1)
    player_key: str | None = None


class RoomCreated(BaseModel):
    code: str
    url: str
    room: dict[str, Any]


@router.get("", summary="Публичные комнаты")
async def list_rooms(rooms: RoomsDep, user: UserDep) -> dict[str, Any]:
    return {
        "rooms": rooms.public_rooms(),
        "mine": rooms.rooms_of(user.id),
        "total_viewers": rooms.total_viewers,
    }


@router.post("", status_code=status.HTTP_201_CREATED, summary="Создать комнату")
async def create_room(
    payload: CreateRoomRequest,
    rooms: RoomsDep,
    catalog: CatalogDep,
    playback: PlaybackDep,
    settings: SettingsDep,
    user: UserDep,
) -> RoomCreated:
    source = None
    if payload.anime_id:
        card = None
        try:
            card = await catalog.card(payload.anime_id)
        except Exception:  # карточка — приятный бонус, без неё тоже можно смотреть
            card = None
        info = await playback.build_source(
            payload.anime_id, payload.episode, player_key=payload.player_key, card=card
        )
        source = info.to_dict()

    room = rooms.create(
        host=user,
        name=payload.name,
        is_public=payload.is_public,
        control_mode=payload.control_mode,
        source=source,
    )
    return RoomCreated(
        code=room.code,
        url=f"{settings.base_url}/room/{room.code}",
        room=room.public_info(),
    )


@router.get("/{code}", summary="Состояние комнаты")
async def room_state(code: str, rooms: RoomsDep, user: UserDep) -> dict[str, Any]:
    room = rooms.get(code)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Комната не найдена или уже закрылась.")
    return room.snapshot()


@router.delete("/{code}", summary="Закрыть комнату (только владелец)")
async def close_room(code: str, rooms: RoomsDep, user: UserDep) -> dict[str, str]:
    room = rooms.get(code)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Комната не найдена.")
    if room.host_id != user.id and not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Закрыть комнату может только владелец.")
    await room.broadcast({"type": "closed", "reason": "Владелец закрыл комнату."})
    rooms.remove(room.code)
    return {"status": "closed"}
