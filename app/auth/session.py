"""Сессия пользователя: что лежит в cookie и как из этого получить запись в БД."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import User

SESSION_KEY = "user"


@dataclass(frozen=True)
class SessionUser:
    """Пользователь, восстановленный из cookie. В БД за ним стоит :class:`User`."""

    id: int
    provider: str
    external_id: str
    username: str
    display_name: str
    avatar_url: str | None = None
    is_admin: bool = False

    @property
    def is_guest(self) -> bool:
        return self.provider == "guest"

    @property
    def initial(self) -> str:
        return (self.display_name or "?")[:1].upper()

    def to_session(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "external_id": self.external_id,
            "username": self.username,
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
            "is_admin": self.is_admin,
        }

    def public(self) -> dict[str, Any]:
        """То, что можно показать другим зрителям в комнате."""
        return {
            "id": self.id,
            "name": self.display_name,
            "avatar": self.avatar_url,
            "provider": self.provider,
        }

    @classmethod
    def from_session(cls, payload: dict[str, Any] | None) -> SessionUser | None:
        if not payload:
            return None
        try:
            return cls(
                id=int(payload["id"]),
                provider=str(payload["provider"]),
                external_id=str(payload["external_id"]),
                username=str(payload["username"]),
                display_name=str(payload["display_name"]),
                avatar_url=payload.get("avatar_url"),
                is_admin=bool(payload.get("is_admin", False)),
            )
        except (KeyError, TypeError, ValueError):
            return None  # cookie от старой версии — считаем, что пользователь не вошёл

    @classmethod
    def from_model(cls, user: User) -> SessionUser:
        return cls(
            id=user.id,
            provider=user.provider,
            external_id=user.external_id,
            username=user.username,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            is_admin=user.is_admin,
        )


async def upsert_user(
    db: AsyncSession,
    *,
    provider: str,
    external_id: str,
    username: str,
    display_name: str,
    avatar_url: str | None = None,
    is_admin: bool = False,
) -> User:
    """Создаёт пользователя или обновляет его профиль при повторном входе."""
    result = await db.execute(
        select(User).where(User.provider == provider, User.external_id == external_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        user = User(provider=provider, external_id=external_id)
        db.add(user)
    user.username = username[:64]
    user.display_name = (display_name or username)[:64]
    user.avatar_url = avatar_url
    user.is_admin = is_admin
    await db.commit()
    await db.refresh(user)
    return user


async def login_guest(db: AsyncSession, display_name: str, settings: Settings) -> SessionUser:
    """Гостевой вход: без Discord, только имя. Доступен, когда OAuth выключен."""
    if settings.discord_auth_enabled:
        raise PermissionError("Гостевой вход выключен: включена авторизация через Discord.")
    name = (display_name or "").strip()[:32] or "Гость"
    user = await upsert_user(
        db,
        provider="guest",
        external_id=secrets.token_hex(8),
        username=name,
        display_name=name,
        avatar_url=None,
        is_admin=False,
    )
    return SessionUser.from_model(user)
