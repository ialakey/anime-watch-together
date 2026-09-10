"""Модели БД: пользователи, список аниме и прогресс по эпизодам."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Базовый класс всех моделей."""


class WatchStatus(enum.StrEnum):
    """Статус тайтла в личном списке."""

    PLANNED = "planned"
    WATCHING = "watching"
    COMPLETED = "completed"
    ON_HOLD = "on_hold"
    DROPPED = "dropped"

    @property
    def title_ru(self) -> str:
        return _STATUS_TITLES[self]


_STATUS_TITLES: dict[WatchStatus, str] = {
    WatchStatus.PLANNED: "Запланировано",
    WatchStatus.WATCHING: "Смотрю",
    WatchStatus.COMPLETED: "Просмотрено",
    WatchStatus.ON_HOLD: "Отложено",
    WatchStatus.DROPPED: "Брошено",
}

#: Хранится строковое значение (``planned``), а не имя члена (``PLANNED``).
watch_status_type = Enum(
    WatchStatus,
    name="watch_status",
    native_enum=False,
    length=16,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


class User(Base):
    """Пользователь сайта: Discord-аккаунт либо локальный гость."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(16), default="guest")
    """``discord`` или ``guest``."""
    external_id: Mapped[str] = mapped_column(String(64))
    """Discord ID либо сгенерированный идентификатор гостя."""
    username: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(64))
    avatar_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    entries: Mapped[list[WatchlistEntry]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    progress: Mapped[list[EpisodeProgress]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_users_provider_external_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочный помощник
        return f"<User {self.provider}:{self.external_id} {self.display_name!r}>"


class WatchlistEntry(Base):
    """Тайтл в личном списке пользователя."""

    __tablename__ = "watchlist_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(16), default="animego")
    anime_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(255))
    poster_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[WatchStatus] = mapped_column(
        watch_status_type, default=WatchStatus.WATCHING, index=True
    )
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Личная оценка 1..10."""
    episodes_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_episode: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="entries")

    __table_args__ = (
        UniqueConstraint("user_id", "source", "anime_id", name="uq_watchlist_user_anime"),
        Index("ix_watchlist_user_status", "user_id", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочный помощник
        return f"<WatchlistEntry {self.anime_id} {self.status} ep={self.last_episode}>"


class EpisodeProgress(Base):
    """Прогресс по конкретному эпизоду: сколько просмотрено и досмотрен ли он."""

    __tablename__ = "episode_progress"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(16), default="animego")
    anime_id: Mapped[str] = mapped_column(String(128))
    episode: Mapped[int] = mapped_column(Integer)
    position: Mapped[float] = mapped_column(Float, default=0.0)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    translation: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="progress")

    __table_args__ = (
        UniqueConstraint(
            "user_id", "source", "anime_id", "episode", name="uq_progress_user_anime_episode"
        ),
        Index("ix_progress_user_updated", "user_id", "updated_at"),
    )

    @property
    def percent(self) -> int:
        if not self.duration:
            return 100 if self.completed else 0
        return max(0, min(100, round(self.position / self.duration * 100)))

    def __repr__(self) -> str:  # pragma: no cover - отладочный помощник
        return f"<EpisodeProgress {self.anime_id} ep{self.episode} {self.position:.0f}s>"
