"""Состояние комнаты: позиция, права управления, зрители, чат."""

from __future__ import annotations

import time

import pytest

from app.auth.session import SessionUser
from app.config import Settings
from app.services.rooms import (
    _CODE_ALPHABET,
    _CODE_LENGTH,
    Playback,
    RoomError,
    RoomManager,
    generate_code,
)


def make_user(user_id: int = 1, name: str = "Тестер", admin: bool = False) -> SessionUser:
    return SessionUser(
        id=user_id,
        provider="guest",
        external_id=str(user_id),
        username=name,
        display_name=name,
        is_admin=admin,
    )


@pytest.fixture
def manager() -> RoomManager:
    return RoomManager(Settings(secret_key="x"))


def test_playback_position_moves_while_playing() -> None:
    playback = Playback(playing=True, position=10.0, updated_at=time.time() - 5)
    assert playback.current() == pytest.approx(15.0, abs=0.2)


def test_paused_playback_stays_put() -> None:
    playback = Playback(playing=False, position=10.0, updated_at=time.time() - 5)
    assert playback.current() == pytest.approx(10.0)


def test_apply_without_position_freezes_current_moment() -> None:
    playback = Playback(playing=True, position=10.0, updated_at=time.time() - 3)
    playback.apply(playing=False)
    assert playback.playing is False
    assert playback.position == pytest.approx(13.0, abs=0.2)


def test_seek_keeps_playing_state() -> None:
    playback = Playback(playing=True, position=1.0)
    playback.apply(position=120.0)
    assert playback.playing is True
    assert playback.position == 120.0


def test_room_codes_are_unambiguous() -> None:
    codes = {generate_code() for _ in range(200)}
    # проверяем принадлежность алфавиту, а не islower(): код может целиком
    # состоять из цифр, и тогда islower() вернёт False, хотя код корректный
    alphabet = set(_CODE_ALPHABET)
    assert all(set(code) <= alphabet for code in codes)
    assert all(len(code) == _CODE_LENGTH for code in codes)
    # ни одного символа, который путают на слух и на глаз
    assert not set("".join(codes)) & set("01lio")


def test_everyone_can_control_by_default(manager: RoomManager) -> None:
    room = manager.create(host=make_user(1))
    assert room.can_control(make_user(2)) is True


def test_host_only_mode_blocks_guests(manager: RoomManager) -> None:
    room = manager.create(host=make_user(1), control_mode="host")
    assert room.can_control(make_user(1)) is True
    assert room.can_control(make_user(2)) is False
    assert room.can_control(make_user(3, admin=True)) is True
    with pytest.raises(RoomError, match="владелец"):
        room.ensure_can_control(make_user(2))


def test_only_host_changes_settings(manager: RoomManager) -> None:
    room = manager.create(host=make_user(1))
    room.ensure_is_host(make_user(1))
    with pytest.raises(RoomError):
        room.ensure_is_host(make_user(2))


def test_chat_keeps_only_recent_messages() -> None:
    settings = Settings(secret_key="x", room_chat_history=3)
    manager = RoomManager(settings)
    room = manager.create(host=make_user(1))
    for index in range(6):
        room.add_chat_message(user_id=1, user_name="Т", avatar=None, text=f"сообщение {index}")
    assert len(room.chat) == 3
    assert room.chat[-1].text == "сообщение 5"
    assert [message.id for message in room.chat] == [4, 5, 6]


def test_public_rooms_hide_empty_and_private(manager: RoomManager) -> None:
    public = manager.create(host=make_user(1), is_public=True)
    manager.create(host=make_user(2), is_public=False)
    assert manager.public_rooms() == []  # без зрителей комнату не показываем

    public.connections["c1"] = _connection(make_user(1))
    codes = [room["code"] for room in manager.public_rooms()]
    assert codes == [public.code]


def test_room_capacity_is_enforced() -> None:
    manager = RoomManager(Settings(secret_key="x", room_max_members=2))
    room = manager.create(host=make_user(1))
    room.add_connection(_connection(make_user(1), "a"))
    room.add_connection(_connection(make_user(2), "b"))
    # вторая вкладка того же зрителя место не занимает
    room.add_connection(_connection(make_user(1), "c"))
    assert room.member_count == 2
    with pytest.raises(RoomError, match="мест"):
        room.add_connection(_connection(make_user(3), "d"))


def test_members_are_deduplicated_by_user(manager: RoomManager) -> None:
    room = manager.create(host=make_user(1, "Хост"))
    room.add_connection(_connection(make_user(1, "Хост"), "a"))
    room.add_connection(_connection(make_user(1, "Хост"), "b"))
    room.add_connection(_connection(make_user(2, "Гость"), "c"))
    members = room.members
    assert len(members) == 2
    assert members[0]["is_host"] is True
    assert members[0]["tabs"] == 2


def test_snapshot_has_everything_the_client_needs(manager: RoomManager) -> None:
    room = manager.create(host=make_user(1), name="Вечер аниме")
    snapshot = room.snapshot()
    assert snapshot["name"] == "Вечер аниме"
    assert set(snapshot) >= {"code", "members", "playback", "source", "chat", "control_mode"}
    assert snapshot["playback"]["playing"] is False


def _connection(user: SessionUser, connection_id: str = "c1"):
    from app.services.rooms import Connection

    class _Socket:
        async def send_json(self, message: dict) -> None:  # pragma: no cover - заглушка
            return None

    return Connection(id=connection_id, user=user, websocket=_Socket())  # type: ignore[arg-type]


class _CollectingSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


async def test_broadcast_skips_the_initiator(manager: RoomManager) -> None:
    from app.services.rooms import Connection

    room = manager.create(host=make_user(1))
    first, second = _CollectingSocket(), _CollectingSocket()
    room.connections["a"] = Connection(id="a", user=make_user(1), websocket=first)  # type: ignore[arg-type]
    room.connections["b"] = Connection(id="b", user=make_user(2), websocket=second)  # type: ignore[arg-type]

    await room.broadcast({"type": "playback"}, exclude="a")
    assert first.sent == []
    assert second.sent == [{"type": "playback"}]


async def test_dead_connections_are_dropped(manager: RoomManager) -> None:
    from app.services.rooms import Connection

    class _BrokenSocket:
        async def send_json(self, message: dict) -> None:
            raise RuntimeError("соединение закрыто")

    room = manager.create(host=make_user(1))
    room.connections["dead"] = Connection(id="dead", user=make_user(1), websocket=_BrokenSocket())  # type: ignore[arg-type]
    await room.broadcast({"type": "ping"})
    assert "dead" not in room.connections
